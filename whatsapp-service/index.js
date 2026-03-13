require('dotenv').config()
const express = require('express')
const cors = require('cors')
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    jidDecode,
    Browsers,
    downloadMediaMessage
} = require('@whiskeysockets/baileys')
const { GoogleGenerativeAI } = require('@google/generative-ai')
const { createClient } = require('@supabase/supabase-js')
const QRCode = require('qrcode')
const pino = require('pino')
const path = require('path')
const fs = require('fs')
// fetch nativo do Node 20 — sem necessidade de node-fetch ou form-data

const app = express()
app.use(cors())
app.use(express.json({ limit: '50mb' }))

// ── Supabase ──
const supabase = createClient(
    process.env.SUPABASE_URL || '',
    process.env.SUPABASE_SERVICE_KEY || process.env.SUPABASE_KEY || ''
)

const ADMIN_TENANT_ID = process.env.ADMIN_TENANT_ID || 'admin'

// ── Sessões Multi-Tenant ──
const sessions = new Map()

// ── Buffer de mensagens por conversa ──
const conversationBuffers = new Map()
const MAX_BUFFER_MESSAGES = 6

// ── Throttle de envio proativo (evita spam) ──
const lastProactiveTime = new Map()
const PROACTIVE_COOLDOWN_MS = 45000 // 45s mínimo entre intervenções proativas

// ── Cache de versão Baileys ──
let cachedBaileysVersion = null

async function getBaileysVersion() {
    if (cachedBaileysVersion) return cachedBaileysVersion
    try {
        const v = await fetchLatestBaileysVersion()
        cachedBaileysVersion = v
        return v
    } catch {
        return { version: [2, 3000, 1033846690], isLatest: true }
    }
}

process.on('uncaughtException', (err) => console.error('❌ UncaughtException:', err?.message))
process.on('unhandledRejection', (r) => console.error('❌ UnhandledRejection:', r?.message || r))

// ══════════════════════════════════════════════════
// MÓDULO DE ÁUDIO — STT (Speech-to-Text)
// ══════════════════════════════════════════════════
async function transcribeAudio(audioBuffer, mimeType, configs) {
    const sttCfg = configs.stt || {}
    const provider = sttCfg.provider || 'groq'
    const apiKey = sttCfg.api_key || configs.api_key || ''

    if (!apiKey) {
        console.warn('[STT] Sem API key configurada.')
        return null
    }

    try {
        // Salva em disco e libera buffer da RAM imediatamente
        const tmpPath = path.join('/tmp', `audio_${Date.now()}.ogg`)
        fs.writeFileSync(tmpPath, audioBuffer)
        audioBuffer = null // libera RAM

        let apiUrl = 'https://api.groq.com/openai/v1/audio/transcriptions'
        const model = provider === 'openai'
            ? (sttCfg.model || 'whisper-1')
            : (sttCfg.model || 'whisper-large-v3-turbo')
        if (provider === 'openai') apiUrl = 'https://api.openai.com/v1/audio/transcriptions'

        // FormData nativo do Node 20 + Blob — compatível com fetch nativo, sem form-data lib
        const fileBytes = fs.readFileSync(tmpPath)
        const blob = new Blob([fileBytes], { type: mimeType || 'audio/ogg' })
        const form = new globalThis.FormData()
        form.append('file', blob, 'audio.ogg')
        form.append('model', model)
        form.append('language', 'pt')
        form.append('response_format', 'text')

        const resp = await fetch(apiUrl, {
            method: 'POST',
            headers: { Authorization: `Bearer ${apiKey}` },
            body: form
        })

        try { fs.unlinkSync(tmpPath) } catch (_) {}

        if (!resp.ok) {
            const err = await resp.text()
            console.error('[STT] Erro na transcrição:', err)
            return null
        }

        const text = await resp.text()
        console.log(`[STT] ✅ Transcrição: "${text.trim()}"`)
        return text.trim()
    } catch (err) {
        console.error('[STT] Exceção:', err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// MÓDULO DE VISÃO — Análise de Imagem
// ══════════════════════════════════════════════════
async function analyzeImage(imageBuffer, caption, configs) {
    const visionCfg = configs.vision || {}
    const provider = visionCfg.provider || configs.ai_provider || 'gemini'
    const apiKey = visionCfg.api_key || configs.api_key || ''
    const model = visionCfg.model || 'gemini-2.0-flash'

    if (!apiKey) {
        console.warn('[VISION] Sem API key configurada.')
        return null
    }

    try {
        // Reduz imagem grande antes de base64 (>1MB -> escala pra 800px max)
        // Isso evita base64 de 3-4MB em RAM
        let imgData = imageBuffer
        if (imageBuffer.length > 800_000) {
            // Salva temp e usa sharp se disponível, senão usa direto (Gemini aguenta)
            try {
                const sharp = require('sharp')
                imgData = await sharp(imageBuffer).resize(800, 800, { fit: 'inside', withoutEnlargement: true }).jpeg({ quality: 75 }).toBuffer()
                console.log(`[VISION] Imagem reduzida: ${imageBuffer.length} -> ${imgData.length} bytes`)
            } catch (_) { /* sharp nao instalado, usa original */ }
        }
        const base64 = imgData.toString('base64')
        imgData = null       // libera copia reduzida
        imageBuffer = null   // libera original da RAM
        const question = caption ? `Caption da imagem: "${caption}". Descreva o que vê e comente sobre o assunto.` : 'Descreva detalhadamente esta imagem.'

        if (provider === 'gemini') {
            const genAI = new GoogleGenerativeAI(apiKey)
            const mdl = genAI.getGenerativeModel({ model })
            const result = await mdl.generateContent([
                { inlineData: { mimeType: 'image/jpeg', data: base64 } },
                question
            ])
            const description = result.response.text()
            console.log(`[VISION] ✅ Gemini analisou imagem: "${description.substring(0, 80)}..."`)
            return description
        }

        // OpenAI-compatible (OpenRouter, NVIDIA, etc.)
        let apiUrl = 'https://openrouter.ai/api/v1/chat/completions'
        if (provider === 'openai') apiUrl = 'https://api.openai.com/v1/chat/completions'
        if (provider === 'nvidia') apiUrl = 'https://integrate.api.nvidia.com/v1/chat/completions'

        const resp = await fetch(apiUrl, {
            method: 'POST',
            headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model,
                messages: [{
                    role: 'user',
                    content: [
                        { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${base64}` } },
                        { type: 'text', text: question }
                    ]
                }]
            })
        })
        const data = await resp.json()
        const description = data.choices?.[0]?.message?.content
        console.log(`[VISION] ✅ OpenAI-compat analisou imagem.`)
        return description || null
    } catch (err) {
        console.error('[VISION] Exceção:', err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// MÓDULO DE VOZ — TTS (Edge TTS — Microsoft, zero custo)
// ══════════════════════════════════════════════════

// Vozes disponíveis pt-BR no edge-tts (versão atual)
const VALID_EDGE_VOICES_PTBR = [
    'pt-BR-FranciscaNeural',
    'pt-BR-AntonioNeural',
    'pt-BR-ThalitaMultilingualNeural',
]

// Configurações de prosódia por voz para tom amigável/humanizado/descontraído
const EDGE_VOICE_STYLE = {
    'pt-BR-FranciscaNeural':           { rate: '-5%', pitch: '+2Hz' },
    'pt-BR-AntonioNeural':             { rate: '-5%', pitch: '-1Hz' },
    'pt-BR-ThalitaMultilingualNeural': { rate:  '0%', pitch: '+3Hz' },
}

/**
 * Remove emojis e símbolos não-verbais do texto antes do TTS.
 * Evita que a IA leia "Coração Vermelho", "Rosto Sorrindo", etc.
 */
function cleanTextForTTS(text) {
    if (!text) return ''
    let clean = text
    // Remove emojis Unicode (range completo: emoticons, símbolos, transporte, misc)
    clean = clean.replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}\u{2300}-\u{23FF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{1FA00}-\u{1FAFF}]/gu, ' ')
    // Remove variações de emoji e ZWJ sequences
    clean = clean.replace(/[\uFE0F\u200D\u20E3]/g, '')
    // Remove marcações WhatsApp (negrito, itálico, tachado, código)
    clean = clean.replace(/[*_~`]/g, '')
    // Colapsa espaços múltiplos
    clean = clean.replace(/\s{2,}/g, ' ').trim()
    // Limita tamanho (PTT ideal até ~500 chars)
    return clean.substring(0, 500)
}

async function generateAudio(text, configs) {
    const ttsCfg = configs.tts || {}
    const ttsEnabled = ttsCfg.enabled === true || ttsCfg.enabled === 'true'
    if (!ttsEnabled) return null

    const provider = ttsCfg.provider || 'edge'

    // Limpa texto (remove emojis, markdown)
    const cleanText = cleanTextForTTS(text)
    if (!cleanText) return null

    try {
        const { execFile, exec } = require('child_process')
        const { promisify } = require('util')
        const execFileAsync = promisify(execFile)
        const execAsync = promisify(exec)

        const tmpMp3 = path.join('/tmp', `tts_${Date.now()}.mp3`)
        const tmpWav = path.join('/tmp', `tts_${Date.now()}.wav`)
        const tmpOgg = path.join('/tmp', `tts_${Date.now() + 1}.ogg`)

        let generated = false

        // ── Edge-TTS ──
        if (provider === 'edge') {
            // Valida voz — substitui vozes obsoletas por Francisca
            let voiceId = ttsCfg.voice_id || 'pt-BR-FranciscaNeural'
            if (!VALID_EDGE_VOICES_PTBR.includes(voiceId)) {
                console.warn(`[TTS] Voz "${voiceId}" não existe mais, usando pt-BR-FranciscaNeural`)
                voiceId = 'pt-BR-FranciscaNeural'
            }

            const voiceStyle = EDGE_VOICE_STYLE[voiceId] || { rate: '-5%', pitch: '+1Hz' }
            const safeText = cleanText.replace(/"/g, '\\"')

            try {
                console.log(`[TTS] Edge-TTS → ${voiceId} | rate:${voiceStyle.rate} pitch:${voiceStyle.pitch}`)

                // Usa --rate e --pitch para tom humanizado e descontraído
                await execAsync(
                    `edge-tts --voice "${voiceId}" --rate "${voiceStyle.rate}" --pitch "${voiceStyle.pitch}" --text "${safeText}" --write-media "${tmpMp3}"`,
                    { timeout: 25000 }
                )

                if (fs.existsSync(tmpMp3)) {
                    await execFileAsync('ffmpeg', [
                        '-y', '-i', tmpMp3,
                        '-c:a', 'libopus',
                        '-b:a', '32k',
                        '-vbr', 'on',
                        tmpOgg
                    ], { timeout: 15000 })
                    try { fs.unlinkSync(tmpMp3) } catch (_) {}
                    generated = true
                }
            } catch (err) {
                console.error('[TTS] Edge-TTS falhou:', err.message)
                // Fallback: retorna null → sendSmartResponse envia texto
                return null
            }
        }

        // ── espeak-ng — APENAS se selecionado explicitamente no dashboard ──
        if (provider === 'espeak') {
            console.log(`[TTS] espeak-ng (selecionado no dashboard)`)
            try {
                await execFileAsync('espeak-ng', [
                    '-v', 'pt-br', '-s', '155', '-p', '60', '-a', '180',
                    '-w', tmpWav, cleanText
                ], { timeout: 15000 })

                if (fs.existsSync(tmpWav)) {
                    await execFileAsync('ffmpeg', ['-y', '-i', tmpWav, '-c:a', 'libopus', '-b:a', '24k', '-vbr', 'on', tmpOgg], { timeout: 15000 })
                    try { fs.unlinkSync(tmpWav) } catch (_) {}
                    generated = true
                }
            } catch (err) {
                console.error('[TTS] espeak-ng falhou:', err.message)
                return null
            }
        }

        if (!generated || !fs.existsSync(tmpOgg)) {
            return null
        }

        const audioBuffer = fs.readFileSync(tmpOgg)
        try { fs.unlinkSync(tmpOgg) } catch (_) {}

        console.log(`[TTS] ✅ Áudio PTT gerado (${audioBuffer.length} bytes) via ${provider}`)
        return audioBuffer
    } catch (err) {
        console.error('[TTS] Exceção geral no módulo de voz:', err.message)
        return null
    }
}
// ══════════════════════════════════════════════════
// DECIDE SE ENVIA TEXTO, ÁUDIO OU AMBOS
// (com indicadores de presença: "digitando" / "gravando áudio")
// ══════════════════════════════════════════════════

/**
 * Simula delay humano proporcional ao tamanho do texto.
 * "Digitando" ~40 chars/s, "Gravando" 1-3s fixo.
 */
async function humanDelay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms))
}

async function sendSmartResponse(sock, remoteJid, text, quotedMsg, configs) {
    const ttsCfg = configs.tts || {}
    const ttsEnabled = ttsCfg.enabled === true || ttsCfg.enabled === 'true'

    // Decide se vai enviar áudio (randomicamente ou por config)
    const shouldSendAudio = ttsEnabled && Math.random() < (parseFloat(ttsCfg.audio_probability) || 0.3)

    if (shouldSendAudio && text.length < 500) {
        try {
            // Mostra "gravando áudio..." antes de gerar
            await sock.sendPresenceUpdate('recording', remoteJid)

            const audioBuffer = await generateAudio(text, configs)

            if (audioBuffer) {
                // Pequeno delay natural após "gravação"
                await humanDelay(800 + Math.random() * 1200)
                await sock.sendPresenceUpdate('available', remoteJid)

                await sock.sendMessage(remoteJid, {
                    audio: audioBuffer,
                    mimetype: 'audio/ogg; codecs=opus',
                    ptt: true
                }, { quoted: quotedMsg })
                console.log(`[SEND] ✅ Áudio PTT enviado para ${remoteJid}`)
                return
            }
            // Se geração de áudio falhou → cai para envio de texto abaixo
            await sock.sendPresenceUpdate('available', remoteJid)
        } catch (presErr) {
            console.warn('[SEND] Erro ao atualizar presença:', presErr.message)
        }
    }

    // Envia texto com indicador "digitando..."
    try {
        await sock.sendPresenceUpdate('composing', remoteJid)
        // Delay proporcional: ~35ms por caractere, mín 800ms, máx 4000ms
        const typingMs = Math.min(4000, Math.max(800, text.length * 35))
        await humanDelay(typingMs)
        await sock.sendPresenceUpdate('available', remoteJid)
    } catch (_) {}

    await sock.sendMessage(remoteJid, { text }, { quoted: quotedMsg })
}

// ══════════════════════════════════════════════════
// CORE DA IA — Geração de Resposta
// ══════════════════════════════════════════════════
async function getAIResponse(prompt, configs, overrideSystemPrompt = null) {
    const chatCfg = configs.chat || {}
    const provider = chatCfg.provider || configs.ai_provider || 'gemini'
    const apiKey = chatCfg.api_key || configs.api_key || ''
    const model = chatCfg.model || configs.model || ''
    const systemPrompt = overrideSystemPrompt || chatCfg.system_prompt || configs.system_prompt || 'Você é um assistente virtual.'

    if (!apiKey || !model) {
        console.warn(`[AI] Config incompleta para provider ${provider}.`)
        return null
    }

    try {
        if (provider === 'gemini') {
            const genAI = new GoogleGenerativeAI(apiKey)
            const mdl = genAI.getGenerativeModel({ model, systemInstruction: systemPrompt })
            const result = await mdl.generateContent(prompt)
            return result.response.text()
        }

        let apiUrl = ''
        if (provider === 'groq') apiUrl = 'https://api.groq.com/openai/v1/chat/completions'
        else if (provider === 'openrouter') apiUrl = 'https://openrouter.ai/api/v1/chat/completions'
        else if (provider === 'nvidia') apiUrl = 'https://integrate.api.nvidia.com/v1/chat/completions'
        else if (provider === 'openai') apiUrl = 'https://api.openai.com/v1/chat/completions'
        else if (provider === 'kimi' || provider === 'moonshot') apiUrl = 'https://api.moonshot.ai/v1/chat/completions'
        else if (provider === 'deepseek') apiUrl = 'https://api.deepseek.com/v1/chat/completions'
        else if (provider === 'ollama') {
            const ollamaUrl = process.env.OLLAMA_PROXY_URL || 'http://automais.ddns.net:11434'
            apiUrl = `${ollamaUrl}/v1/chat/completions`
        } else {
            return null
        }

        const resp = await fetch(apiUrl, {
            method: 'POST',
            headers: {
                ...(provider !== 'ollama' ? { Authorization: `Bearer ${apiKey}` } : {}),
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://redcomercial.com.br',
                'X-Title': 'Red Comercial AI'
            },
            body: JSON.stringify({
                model,
                messages: [
                    { role: 'system', content: systemPrompt },
                    { role: 'user', content: prompt }
                ],
                max_tokens: 1024,
                temperature: 0.85
            })
        })

        const data = await resp.json()
        if (data.error) {
            console.error(`[AI] Erro ${provider}:`, data.error)
            return null
        }
        return data.choices?.[0]?.message?.content || null
    } catch (err) {
        console.error(`[AI] Exceção (${provider}):`, err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// APRENDIZADO CONTÍNUO — Rolling Summary + Perfis
// ══════════════════════════════════════════════════
async function learnFromConversation(tenantId, conversationId, newMessages, aiConfigs) {
    const chatCfg = aiConfigs.chat || {}
    const apiKey = chatCfg.api_key || aiConfigs.api_key || ''
    if (!apiKey) return

    console.log(`[LEARN] 🧠 Analisando dinâmica de ${conversationId}...`)

    try {
        const { data: currentContext } = await supabase
            .from('whatsapp_conversation_contexts')
            .select('*')
            .eq('tenant_id', tenantId)
            .eq('conversation_id', conversationId)
            .single()

        const oldSummary = currentContext?.summary || 'Sem histórico anterior.'
        const transcript = newMessages.map(m => `${m.author} (${m.authorJid}): ${m.text}`).join('\n')

        const chatPersonality = (aiConfigs.chat || {}).system_prompt || 'Você é um membro humano do grupo.'

        const prompt = `Você é um especialista em análise social, comportamento humano e grupos de WhatsApp.

PERSONALIDADE DA IA NO GRUPO: "${chatPersonality}"
RESUMO ANTERIOR: "${oldSummary}"
NOVAS MENSAGENS:
${transcript}

Analise e retorne APENAS um JSON puro (sem markdown) com:
1. "summary": Resumo completo e atualizado (histórico + novidades).
2. "vibe": Humor atual (ex: "Zoeira total", "Discussão acalorada", "Animado", "Cotidiano tranquilo").
3. "group_type": Tipo do grupo (ex: "Família", "Amigos", "Trabalho", "Galera do rolê").
4. "daily_topics": Tópicos principais agora.
5. "style": Gírias e expressões usadas no grupo (ex: "bicho, vixe, macho, eita, oxe").
6. "profiles": Array de { "jid": "...", "name": "...", "nicknames": [], "personality": "...", "style_note": "..." }
7. "proactive_thought": Fala CURTA e NATURAL da IA para entrar na conversa AGORA como um humano do grupo faria. Use gírias do "style". Preencha quando: algo engraçado aconteceu, discussão polêmica, pergunta aberta, assunto que a IA domina, celebração. NUNCA use saudações formais. Máximo 2-3 frases. Deixe VAZIO se a conversa for trivial ou não houver ganho em participar.
8. "proactive_urgency": Número 0-10 de urgência para participar agora (0=deixa rolar, 7+=vale entrar, 10=imperdível).
9. "proactive_trigger": Gatilho identificado (ex: "pergunta aberta", "piada", "polêmica", "celebração"). Vazio se nenhum.
10. "context_for_next_response": Uma frase do que a IA precisa saber para responder bem quando for chamada.

Retorne APENAS o JSON puro.`

        // Usa provider da proatividade se configurado, senão fallback para chat
        const proactiveCfgForAI = aiConfigs.proactive || {}
        const learningChatCfg = (proactiveCfgForAI.provider && proactiveCfgForAI.api_key && proactiveCfgForAI.model)
            ? { provider: proactiveCfgForAI.provider, api_key: proactiveCfgForAI.api_key, model: proactiveCfgForAI.model, system_prompt: chatCfg.system_prompt }
            : { ...chatCfg }
        const analysisCfg = { ...aiConfigs, chat: learningChatCfg }
        const aiResponse = await getAIResponse(prompt, analysisCfg,
            'Você é um observador silencioso e analista de comportamento. Responda apenas com JSON puro.')

        if (!aiResponse) return

        const clean = aiResponse.replace(/```json|```/g, '').trim()
        const result = JSON.parse(clean)

        // Salva contexto
        await supabase.from('whatsapp_conversation_contexts').upsert({
            tenant_id: tenantId,
            conversation_id: conversationId,
            summary: result.summary || oldSummary,
            vibe: result.vibe || 'Neutro',
            group_type: result.group_type || 'Geral',
            daily_topics: result.daily_topics || '',
            communication_style: result.style || '',
            context_hint: result.context_for_next_response || '',
            updated_at: new Date()
        }, { onConflict: 'tenant_id, conversation_id' })

        // Atualiza perfis de contatos
        if (result.profiles?.length) {
            for (const p of result.profiles) {
                if (!p.jid) continue
                const { data: existing } = await supabase
                    .from('whatsapp_contact_profiles')
                    .select('metadata')
                    .eq('tenant_id', tenantId)
                    .eq('contact_id', p.jid)
                    .single()

                let meta = existing?.metadata || {}
                if (!meta.nicknames) meta.nicknames = []
                if (p.nicknames) p.nicknames.forEach(n => { if (n && !meta.nicknames.includes(n)) meta.nicknames.push(n) })

                await supabase.from('whatsapp_contact_profiles').upsert({
                    tenant_id: tenantId,
                    contact_id: p.jid,
                    full_name: p.name || null,
                    nickname: meta.nicknames[0] || null,
                    personality_traits: p.personality || null,
                    communication_style: p.style_note || null,
                    metadata: meta,
                    updated_at: new Date()
                }, { onConflict: 'tenant_id, contact_id' })
            }
        }

        // ── Intervenção Proativa Inteligente ──
        const urgency = parseFloat(result.proactive_urgency) || 0
        const trigger = result.proactive_trigger || ''
        const proactiveThought = (result.proactive_thought || '').trim()

        const lastProactiveKey = `${tenantId}_${conversationId}`
        const lastProactive = lastProactiveTime.get(lastProactiveKey) || 0
        const timeSinceLastProactive = Date.now() - lastProactive

        const proactiveCfg = aiConfigs.proactive || {}
        const proactiveEnabled = proactiveCfg.enabled !== false && proactiveCfg.enabled !== 'false'
        const frequency = parseFloat(proactiveCfg.frequency || 0.15)

        // Cooldown adaptativo: quanto maior a urgência, menor o cooldown mínimo
        // urgency 10 → 10s cooldown; urgency 5 → 30s; urgency 0 → 45s
        const adaptiveCooldown = urgency >= 9 ? 10000
            : urgency >= 7 ? 20000
            : urgency >= 5 ? 30000
            : PROACTIVE_COOLDOWN_MS

        // Critérios de disparo:
        // 1. Urgência MUITO alta (≥9): quase sempre dispara
        // 2. Urgência alta (≥7) + cooldown OK: dispara
        // 3. Urgência média (≥5) + chance aleatória: pode disparar
        // 4. Qualquer urgência + chance baseada na frequência configurada: dispara às vezes
        const randomRoll = Math.random()
        const shouldParticipate = proactiveEnabled
            && proactiveThought.length > 5
            && timeSinceLastProactive > adaptiveCooldown
            && (
                urgency >= 9
                || (urgency >= 7 && randomRoll < 0.85)
                || (urgency >= 5 && randomRoll < frequency * 2)
                || randomRoll < frequency
            )

        if (shouldParticipate) {
            const session = sessions.get(tenantId)
            if (session?.sock && session.status === 'authenticated') {
                console.log(`[PROATIVO] 🤖 Urgência ${urgency} | Trigger: "${trigger}" | "${proactiveThought.substring(0, 60)}"`)
                lastProactiveTime.set(lastProactiveKey, Date.now())

                // Delay humano: urgência alta → responde rápido, baixa → demora mais
                const minDelay = urgency >= 8 ? 1500 : urgency >= 5 ? 3000 : 5000
                const maxDelay = urgency >= 8 ? 4000 : urgency >= 5 ? 8000 : 15000
                const delay = minDelay + Math.random() * (maxDelay - minDelay)

                setTimeout(async () => {
                    try {
                        await sendSmartResponse(session.sock, conversationId, proactiveThought, null, aiConfigs)
                    } catch (e) {
                        console.error('[PROATIVO] Erro ao enviar:', e.message)
                    }
                }, delay)
            }
        }

        console.log(`[LEARN] ✅ Contexto atualizado para ${conversationId} | Vibe: ${result.vibe}`)
    } catch (err) {
        console.error(`[LEARN] ❌ Erro:`, err.message)
    }
}

// ══════════════════════════════════════════════════
// CARREGAMENTO DE CONFIGS DO TENANT
// ══════════════════════════════════════════════════
async function loadTenantAIConfigs(tenantId) {
    try {
        let configData = {}
        const isAdmin = tenantId === ADMIN_TENANT_ID

        if (isAdmin) {
            const { data, error } = await supabase.from('ai_configs').select('*')
            const configs = {}
            if (!error && data) data.forEach(item => (configs[item.key] = item.value))

            const provider = configs.ai_provider || 'gemini'

            // Configs multi-serviço (novo formato)
            configData = {
                // Chat (principal)
                chat: {
                    provider: configs.chat_provider || provider,
                    api_key: configs[`${configs.chat_provider || provider}_api_key`] || configs[`${provider}_api_key`] || process.env.GEMINI_API_KEY || '',
                    model: configs.chat_model || configs[`${provider}_model`] || 'gemini-2.0-flash',
                    system_prompt: configs.chat_system_prompt || configs[`${provider}_system_prompt`] || 'Você é o assistente RED.IA da RED Corporation.'
                },
                // STT (transcrição de áudio)
                stt: {
                    provider: configs.stt_provider || 'groq',
                    api_key: configs.stt_api_key || configs.groq_api_key || '',
                    model: configs.stt_model || 'whisper-large-v3-turbo',
                    enabled: configs.stt_enabled !== 'false'
                },
                // Visão (análise de imagens)
                vision: {
                    provider: configs.vision_provider || 'gemini',
                    api_key: configs.vision_api_key || configs.gemini_api_key || process.env.GEMINI_API_KEY || '',
                    model: configs.vision_model || 'gemini-2.0-flash',
                    enabled: configs.vision_enabled !== 'false'
                },
                // TTS (síntese de voz)
                tts: {
                    provider: configs.tts_provider || 'edge',
                    api_key: configs.tts_api_key || '',
                    model: configs.tts_model || '',
                    voice_id: configs.tts_voice_id || 'pt-BR-FranciscaNeural',
                    enabled: configs.tts_enabled === 'true',
                    audio_probability: parseFloat(configs.tts_audio_probability) || 0.3
                },
                // Aprendizado / Memória
                learning: {
                    provider: configs.learning_provider || configs.chat_provider || provider,
                    api_key: configs.learning_api_key || configs[`${provider}_api_key`] || '',
                    model: configs.learning_model || configs[`${provider}_model`] || 'gemini-2.0-flash',
                    enabled: configs.learning_enabled !== 'false'
                },
                // Proatividade
                proactive: {
                    enabled: configs.proactive_enabled !== 'false',
                    frequency: parseFloat(configs.proactive_frequency) || 0.15,
                    provider: configs.proactive_provider || configs.chat_provider || provider,
                    api_key: configs.proactive_api_key || configs[`${configs.proactive_provider || configs.chat_provider || provider}_api_key`] || '',
                    model: configs.proactive_model || configs.chat_model || ''
                },
                // Legado (compatibilidade)
                ai_provider: provider,
                api_key: configs[`${provider}_api_key`] || process.env.GEMINI_API_KEY || '',
                model: configs[`${provider}_model`] || 'gemini-2.0-flash',
                system_prompt: configs[`${provider}_system_prompt`] || 'Você é o assistente RED.IA.',
                ai_prefix: configs.ai_prefix || '',
                ai_bot_enabled: configs.ai_bot_enabled === 'true'
            }
        } else {
            const { data: arr } = await supabase.from('whatsapp_tenant_configs').select('*').eq('tenant_id', tenantId).limit(1)
            const d = arr?.[0] || {}

            configData = {
                chat: {
                    provider: d.chat_provider || d.ai_provider || 'gemini',
                    api_key: d.chat_api_key || d.api_key || '',
                    model: d.chat_model || d.model || '',
                    system_prompt: d.system_prompt || 'Você é o assistente virtual da Red Comercial.'
                },
                stt: {
                    provider: d.stt_provider || 'groq',
                    api_key: d.stt_api_key || d.api_key || '',
                    model: d.stt_model || 'whisper-large-v3-turbo',
                    enabled: d.stt_enabled !== false && d.stt_enabled !== 'false'
                },
                vision: {
                    provider: d.vision_provider || 'gemini',
                    api_key: d.vision_api_key || d.api_key || '',
                    model: d.vision_model || 'gemini-2.0-flash',
                    enabled: d.vision_enabled !== false
                },
                tts: {
                    provider: d.tts_provider || 'edge',
                    api_key: d.tts_api_key || '',
                    model: d.tts_model || 'eleven_multilingual_v2',
                    voice_id: d.tts_voice_id || '',
                    enabled: d.tts_enabled === true || d.tts_enabled === 'true',
                    audio_probability: parseFloat(d.tts_audio_probability) || 0.3
                },
                learning: {
                    provider: d.learning_provider || d.ai_provider || 'gemini',
                    api_key: d.learning_api_key || d.api_key || '',
                    model: d.learning_model || d.model || 'gemini-2.0-flash',
                    enabled: d.learning_enabled !== false
                },
                proactive: {
                    enabled: d.proactive_enabled !== false,
                    frequency: parseFloat(d.proactive_frequency) || 0.15,
                    provider: d.proactive_provider || d.ai_provider || 'gemini',
                    api_key: d.proactive_api_key || d.api_key || '',
                    model: d.proactive_model || d.model || ''
                },
                ai_provider: d.ai_provider || 'gemini',
                api_key: d.api_key || '',
                model: d.model || '',
                system_prompt: d.system_prompt || 'Você é o assistente virtual.',
                ai_prefix: d.ai_prefix || '',
                ai_bot_enabled: d.ai_enabled === true
            }
        }

        const session = sessions.get(tenantId)
        if (session) {
            session.aiConfigs = configData
            console.log(`✅ Configs carregadas para Tenant ${tenantId} | Chat: ${configData.chat?.provider}/${configData.chat?.model}`)
        }
    } catch (err) {
        console.error(`Erro ao carregar configs para ${tenantId}:`, err?.message)
        const session = sessions.get(tenantId)
        if (session) {
            session.aiConfigs = {
                chat: { provider: 'gemini', api_key: process.env.GEMINI_API_KEY || '', model: 'gemini-2.0-flash', system_prompt: 'Você é um assistente.' },
                stt: { enabled: false },
                vision: { enabled: false },
                tts: { enabled: false },
                learning: { enabled: false },
                proactive: { enabled: false },
                ai_bot_enabled: false
            }
        }
    }
}

// ══════════════════════════════════════════════════
// RESOLUÇÃO DE NOMES DO WHATSAPP (@lid, @g.us)
// ══════════════════════════════════════════════════
async function resolveNames(text, tenantId, sock) {
    if (!text) return text
    const jidRegex = /(@\d+|@[\w.-]+(@g\.us|@s\.whatsapp\.net|@lid))/g
    const matches = text.match(jidRegex) || []

    let resolvedText = text
    for (const jid of matches) {
        let cleanJid = jid.startsWith('@') ? jid.substring(1) : jid
        if (!cleanJid.includes('@')) {
            cleanJid = cleanJid + (cleanJid.includes('-') ? '@g.us' : '@s.whatsapp.net')
        }

        try {
            // Tenta buscar no cache do Baileys ou Supabase
            let name = null
            
            if (cleanJid.endsWith('@g.us')) {
                const meta = await sock.groupMetadata(cleanJid).catch(() => null)
                name = meta?.subject
            } else {
                const { data: contact } = await supabase.from('whatsapp_contact_profiles')
                    .select('full_name, nickname').eq('tenant_id', tenantId).eq('contact_id', cleanJid).single()
                name = contact?.nickname || contact?.full_name
            }

            if (name) {
                resolvedText = resolvedText.replace(jid, `@${name}`)
            }
        } catch (_) {}
    }
    return resolvedText
}

// ══════════════════════════════════════════════════
// CONTEXTO DA EMPRESA (para tenants comerciais)
// ══════════════════════════════════════════════════
async function getTenantContext(tenantId) {
    if (tenantId === ADMIN_TENANT_ID) return ''
    try {
        const { data: tenant } = await supabase.from('tenants').select('nome, descricao, tipo, endereco, cidade').eq('id', tenantId).single()
        const { data: products } = await supabase.from('products').select('nome, preco, estoque_atual').eq('tenant_id', tenantId).limit(20)

        let ctx = `Empresa: ${tenant?.nome || 'Empresa'}\nRamo: ${tenant?.tipo || 'Comércio'}\nDescrição: ${tenant?.descricao || ''}\nEndereço: ${tenant?.endereco || ''}, ${tenant?.cidade || ''}\n`
        if (products?.length) {
            ctx += '\nPRODUTOS:\n'
            products.forEach(p => { ctx += `- ${p.nome}: R$ ${p.preco?.toFixed(2) || 'Sob consulta'} (Estoque: ${p.estoque_atual || 'N/A'})\n` })
        }
        return ctx
    } catch (err) {
        console.error(`[CONTEXT] Erro:`, err.message)
        return ''
    }
}

// ══════════════════════════════════════════════════
// CONEXÃO WHATSAPP
// ══════════════════════════════════════════════════
async function connectToWhatsApp(tenantId, forceReset = false) {
    console.log(`[WA] Conectando Tenant: ${tenantId}${forceReset ? ' (RESET FORÇADO)' : ''}`)
    const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)

    // Se forceReset, apaga credenciais antigas para garantir novo QR
    if (forceReset && fs.existsSync(authPath)) {
        fs.rmSync(authPath, { recursive: true, force: true })
        console.log(`[WA] 🗑️ Credenciais antigas removidas para ${tenantId}`)
    }

    if (!fs.existsSync(authPath)) fs.mkdirSync(authPath, { recursive: true })

    const { state, saveCreds } = await useMultiFileAuthState(authPath)

    const { version } = await getBaileysVersion()

    const sock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: true,
        browser: Browsers.macOS('Desktop'),
        logger: pino({ level: 'warn' }),
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 25000
    })

    const session = { sock, aiConfigs: null, lastQr: null, status: 'connecting' }
    sessions.set(tenantId, session)

    await loadTenantAIConfigs(tenantId)

    // ── Eventos de conexão ──
    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            session.lastQr = await QRCode.toDataURL(qr)
            session.status = 'qrcode'
            try {
                await supabase.from('whatsapp_sessions').upsert({ tenant_id: tenantId, status: 'qrcode', qr: session.lastQr, updated_at: new Date() }, { onConflict: 'tenant_id' })
            } catch (_) { }
        }

        if (connection === 'close') {
            const statusCode = lastDisconnect?.error?.output?.statusCode
            session.status = 'disconnected'
            session.lastQr = null

            if (statusCode === DisconnectReason.loggedOut || statusCode === 428) {
                sessions.delete(tenantId)
                try { await supabase.from('whatsapp_sessions').delete().eq('tenant_id', tenantId) } catch (_) { }
                if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true })
                if (statusCode === 428) setTimeout(() => connectToWhatsApp(tenantId, false), 3000)
            } else {
                setTimeout(() => connectToWhatsApp(tenantId, false), 2000)
            }
        } else if (connection === 'open') {
            session.status = 'authenticated'
            session.lastQr = null
            try {
                await supabase.from('whatsapp_sessions').upsert({ tenant_id: tenantId, status: 'authenticated', phone: sock.user.id, qr: null, updated_at: new Date() }, { onConflict: 'tenant_id' })
            } catch (_) { }
            console.log(`✅ Tenant ${tenantId} conectado!`)
        }
    })

    sock.ev.on('creds.update', saveCreds)

    // ══════════════════════════════════════════════
    // LISTENER DE MENSAGENS — O CÉREBRO
    // ══════════════════════════════════════════════
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return

        const botJid = sock.user?.id || ''
        const botNumber = jidDecode(botJid)?.user + '@s.whatsapp.net'
        const botLid = sock.user?.lid || ''
        const botId = botNumber.split('@')[0].split(':')[0]
        const botLidShort = botLid.split('@')[0].split(':')[0]

        for (const msg of messages) {
            if (!msg.message || msg.key.fromMe) continue

            const remoteJid = msg.key.remoteJid
            const isGroup = remoteJid.endsWith('@g.us')
            const isPV = !isGroup

            // ── Extração de tipo e conteúdo ──
            const msgType = Object.keys(msg.message)[0]
            let textContent = ''
            let mediaContent = null // { type: 'audio'|'image'|'video'|'document', buffer, mimeType, caption }

            // Texto
            if (msgType === 'conversation') textContent = msg.message.conversation
            else if (msgType === 'extendedTextMessage') textContent = msg.message.extendedTextMessage.text
            else if (msgType === 'buttonsResponseMessage') textContent = msg.message.buttonsResponseMessage.selectedButtonId
            else if (msgType === 'listResponseMessage') textContent = msg.message.listResponseMessage.singleSelectReply.selectedRowId
            else if (msg.message[msgType]?.text) textContent = msg.message[msgType].text
            else if (msg.message[msgType]?.caption) textContent = msg.message[msgType].caption

            // Áudio e PTT
            const isAudio = msgType === 'audioMessage' || (msgType === 'audioMessage' && msg.message.audioMessage?.ptt)
            const isPTT = msgType === 'audioMessage' && msg.message.audioMessage?.ptt

            // Imagem
            const isImage = msgType === 'imageMessage' || msgType === 'viewOnceMessageV2' && msg.message.viewOnceMessageV2?.message?.imageMessage

            // Sticker
            const isSticker = msgType === 'stickerMessage'

            // ── Detecção de menção / resposta ao bot ──
            const contextInfo = msg.message?.extendedTextMessage?.contextInfo ||
                msg.message?.imageMessage?.contextInfo ||
                msg.message?.audioMessage?.contextInfo ||
                msg.message?.ephemeralMessage?.message?.extendedTextMessage?.contextInfo ||
                msg.message?.viewOnceMessageV2?.message?.imageMessage?.contextInfo

            const isMentioned = !!contextInfo?.mentionedJid?.some(jid =>
                jid.includes(botId) || (botLidShort && jid.includes(botLidShort))
            )
            const isReplyToMe = !!(
                contextInfo?.participant?.includes(botId) ||
                (botLidShort && contextInfo?.participant?.includes(botLidShort))
            )

            const configs = session.aiConfigs || {}
            const isBotEnabled = String(configs.ai_bot_enabled) === 'true'

            // ── Palavra-chave (grupos) ──
            const keyword = (configs.ai_prefix || '').trim()
            const normalize = t => t ? t.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase() : ''
            const containsKeyword = Boolean(keyword && normalize(textContent).includes(normalize(keyword)))

            // ══════════════════════════════════
            // BUFFER DE APRENDIZADO (tudo, sempre)
            // ══════════════════════════════════
            const author = msg.pushName || remoteJid.split('@')[0]
            const authorJid = msg.key.participant || remoteJid

            const bufferKey = `${tenantId}_${remoteJid}`
            if (!conversationBuffers.has(bufferKey)) conversationBuffers.set(bufferKey, { tenantId, messages: [] })
            const buffer = conversationBuffers.get(bufferKey)

            const learningEnabled = configs.learning?.enabled !== false
            const sttEnabled = configs.stt?.enabled !== false

            // Adiciona ao buffer (texto OU transcrição de áudio)
            let bufferText = textContent.trim()

            // Pré-processa mídia para o buffer de aprendizado
            if ((isAudio || isImage) && learningEnabled) {
                if (isAudio && sttEnabled) {
                    try {
                        let audioBuffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                        const mimeType = msg.message.audioMessage?.mimetype || 'audio/ogg'
                        const transcription = await transcribeAudio(audioBuffer, mimeType, configs)
                        audioBuffer = null // libera RAM
                        if (transcription) {
                            bufferText = `[AUDIO] ${transcription}`
                            mediaContent = { type: 'audio', transcription }
                        }
                    } catch (e) {
                        console.error('[STT Download] Erro:', e.message)
                    }
                }
                if (isImage && configs.vision?.enabled !== false) {
                    try {
                        let imgBuffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                        const caption = msg.message.imageMessage?.caption || ''
                        const description = await analyzeImage(imgBuffer, caption, configs)
                        if (description) {
                            bufferText = `[IMAGEM] ${description}${caption ? ` | Caption: "${caption}"` : ''}`
                            mediaContent = { type: 'image', description, caption }
                        }
                    } catch (e) {
                        console.error('[VISION Download] Erro:', e.message)
                    }
                }
            }

            if (bufferText.length > 2) {
                buffer.messages.push({ author, authorJid, text: bufferText })
                console.log(`[BUFFER] ${author}: "${bufferText.substring(0, 60)}" (${buffer.messages.length}/${MAX_BUFFER_MESSAGES})`)
            }

            // Dispara aprendizado quando buffer cheio
            if (buffer.messages.length >= MAX_BUFFER_MESSAGES && learningEnabled) {
                const msgs = [...buffer.messages]
                buffer.messages = []
                learnFromConversation(tenantId, remoteJid, msgs, configs).catch(e =>
                    console.error('[LEARN BG] Erro:', e.message)
                )
            }

            // ══════════════════════════════════
            // DECISÃO DE RESPONDER
            // ══════════════════════════════════
            if (!isBotEnabled) continue

            // Em PV: sempre responde (a não ser que seja mídia sem transcrição)
            // Em grupo: responde se mencionado, respondido, keyword, ou via proatividade interna
            const shouldRespond = isPV ||
                (isGroup && (isMentioned || isReplyToMe || containsKeyword))

            // Conteúdo processado para IA
            let contentForAI = textContent
            if (mediaContent?.type === 'audio') contentForAI = `[Mensagem de voz] ${mediaContent.transcription}`
            else if (mediaContent?.type === 'image') contentForAI = `[Imagem enviada] ${mediaContent.description}${textContent ? ` | O usuário também escreveu: "${textContent}"` : ''}`

            // Ignora se não tem conteúdo útil e não foi acionado
            if (!contentForAI.trim() && !mediaContent) continue
            if (!shouldRespond) continue

            console.log(`🤖 IA Respondendo [${isGroup ? 'GRUPO' : 'PV'}] Tenant ${tenantId} → ${remoteJid}`)

            // ── Busca contexto e perfis (RAG) ──
            let convMemory = ''
            let senderProfile = ''
            let currentVibe = 'Neutro'

            try {
                const { data: convData } = await supabase.from('whatsapp_conversation_contexts')
                    .select('summary, vibe, group_type, daily_topics, communication_style, context_hint')
                    .eq('tenant_id', tenantId).eq('conversation_id', remoteJid).single()

                if (convData?.summary) {
                    convMemory = `\n[CONTEXTO: ${convData.summary}]`
                    if (convData.group_type) convMemory += `\n[TIPO: ${convData.group_type}]`
                    if (convData.daily_topics) convMemory += `\n[TÓPICOS: ${convData.daily_topics}]`
                    if (convData.communication_style) convMemory += `\n[ESTILO/GÍRIAS: ${convData.communication_style}]`
                    if (convData.context_hint) convMemory += `\n[DICA PARA RESPOSTA: ${convData.context_hint}]`
                }
                if (convData?.vibe) currentVibe = convData.vibe

                const { data: profData } = await supabase.from('whatsapp_contact_profiles')
                    .select('full_name, nickname, personality_traits, communication_style, metadata')
                    .eq('tenant_id', tenantId).eq('contact_id', authorJid).single()

                if (profData) {
                    const nicks = profData.metadata?.nicknames || []
                    senderProfile = `\n[PERFIL: ${profData.full_name || author}${nicks.length ? `, apelidos: ${nicks.join(', ')}` : ''}, personalidade: ${profData.personality_traits || 'N/A'}, estilo: ${profData.communication_style || 'N/A'}]`
                }
            } catch (_) { }

            // ── Limpa menções e keywords do texto ──
            let cleanText = contentForAI
                .replace(new RegExp(`@${botId}`, 'g'), '')
                .replace(new RegExp(`@${botLidShort}`, 'g'), '')
                .trim()
            if (containsKeyword && keyword) {
                cleanText = cleanText.replace(new RegExp(keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), '').trim()
            }

            // ── Monta prompt ──
            const chatCfg = configs.chat || {}
            const systemPrompt = chatCfg.system_prompt || configs.system_prompt || 'Você é um assistente.'
            const contextEnv = `${convMemory}${senderProfile}`
            const isGroupCtx = isGroup
                ? `\n[CONTEXTO: você está num GRUPO. Seja natural, como membro da galera. Vibe atual: ${currentVibe}]`
                : `\n[CONTEXTO: conversa PRIVADA. Seja atencioso e direto.]`
            const styleInstruction = `\n[ESTILO: Vibe = ${currentVibe}. Use gírias locais se for descontraído. Seja HUMANO, não robótico. Respostas curtas e naturais em grupo, mais detalhadas no PV.]`

            let businessCtx = ''
            if (tenantId !== ADMIN_TENANT_ID) businessCtx = await getTenantContext(tenantId)

            // Resolve nomes no histórico/prompt para a IA
            const resolvedConvMemory = await resolveNames(convMemory, tenantId, sock)
            const resolvedCleanText = await resolveNames(cleanText, tenantId, sock)

            const fullPrompt = `${businessCtx ? `EMPRESA:\n${businessCtx}\n\n` : ''}INSTRUÇÕES:\n${systemPrompt}${resolvedConvMemory}${senderProfile}${isGroupCtx}${styleInstruction}\n\nMENSAGEM DE ${author}: ${resolvedCleanText || 'Oi!'}`

            try {
                const response = await getAIResponse(fullPrompt, configs)
                if (response) {
                    await sendSmartResponse(sock, remoteJid, response, msg, configs)
                } else {
                    if (isPV) await sock.sendMessage(remoteJid, { text: '⚡ Sem conexão com o modelo no momento.' }, { quoted: msg })
                }
            } catch (err) {
                console.error(`[RESP] Erro:`, err.message)
                if (isPV) await sock.sendMessage(remoteJid, { text: 'Erro interno. Tenta de novo!' }, { quoted: msg })
            }
        }
    })
}

// ══════════════════════════════════════════════════
// ENDPOINTS REST
// ══════════════════════════════════════════════════

// Aliases admin
app.get('/status', (_, res) => res.redirect('/status/admin'))
app.post('/start', (_, res) => res.redirect(307, '/start/admin'))
app.post('/stop', (_, res) => res.redirect(307, '/stop/admin'))
app.get('/groups', (_, res) => res.redirect('/groups/admin'))
app.post('/send', (_, res) => res.redirect(307, '/send/admin'))
app.post('/ai/reload', async (_, res) => {
    await loadTenantAIConfigs(ADMIN_TENANT_ID)
    res.json({ success: true })
})

app.get('/status/:tenantId', (req, res) => {
    const s = sessions.get(req.params.tenantId)
    if (!s) return res.json({ status: 'disconnected', qr: null })
    res.json({ status: s.status, qr: s.lastQr })
})

app.post('/start/:tenantId', async (req, res) => {
    try {
        const { tenantId } = req.params
        const existing = sessions.get(tenantId)
        if (existing && existing.status !== 'disconnected' && existing.status !== 'error') {
            return res.json({ success: true, message: 'Sessão já ativa.', status: existing.status })
        }

        // Verifica se há creds.json corrompido/incompleto no volume
        // Se existir mas não tiver creds.json válido, força reset para garantir QR novo
        const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)
        let forceReset = false
        if (fs.existsSync(authPath)) {
            const credsFile = path.join(authPath, 'creds.json')
            if (!fs.existsSync(credsFile)) {
                // Pasta existe mas sem creds — estado corrompido, limpa
                console.log(`[START] ⚠️ Auth dir sem creds.json — limpando para garantir QR`)
                fs.rmSync(authPath, { recursive: true, force: true })
                forceReset = true
            }
        }

        res.json({ success: true, message: 'Iniciando...', status: 'connecting' })
        connectToWhatsApp(tenantId, forceReset).catch(err => console.error(`[BG] Falha ao conectar ${tenantId}:`, err))
    } catch (err) {
        if (!res.headersSent) res.status(500).json({ success: false, error: err.message })
    }
})

app.post('/stop/:tenantId', async (req, res) => {
    const { tenantId } = req.params
    const session = sessions.get(tenantId)
    const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)

    if (session?.sock) {
        try {
            await session.sock.logout()
            res.json({ success: true })
        } catch (e) {
            sessions.delete(tenantId)
            if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true })
            res.json({ success: true, message: 'Limpo após erro.' })
        }
    } else {
        if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true })
        res.json({ success: true })
    }
})

// ── Reset forçado: limpa credenciais e gera novo QR ──
app.post('/reset/:tenantId', async (req, res) => {
    const { tenantId } = req.params
    console.log(`[RESET] 🔄 Reset forçado para tenant: ${tenantId}`)
    const session = sessions.get(tenantId)

    // Encerra sessão existente
    if (session?.sock) {
        try { session.sock.end() } catch (_) {}
    }
    sessions.delete(tenantId)

    // Limpa credenciais do volume
    const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)
    if (fs.existsSync(authPath)) {
        fs.rmSync(authPath, { recursive: true, force: true })
        console.log(`[RESET] 🗑️ Credenciais removidas: ${authPath}`)
    }

    // Limpa estado no Supabase
    try { await supabase.from('whatsapp_sessions').delete().eq('tenant_id', tenantId) } catch (_) {}

    res.json({ success: true, message: 'Sessão resetada. Chame /start para gerar novo QR.' })
})

// Alias de reset para admin
app.post('/reset', (_, res) => res.redirect(307, '/reset/admin'))

app.post('/ai/reload/:tenantId', async (req, res) => {
    await loadTenantAIConfigs(req.params.tenantId)
    res.json({ success: true })
})

app.post('/ai/list-models', async (req, res) => {
    const { api_key, provider } = req.body
    if (!api_key || !provider) return res.status(400).json({ error: 'api_key e provider obrigatórios' })

    try {
        let apiUrl = ''
        let headers = {}

        if (provider === 'gemini') {
            apiUrl = `https://generativelanguage.googleapis.com/v1beta/models?key=${api_key}`
        } else if (provider === 'groq') {
            apiUrl = 'https://api.groq.com/openai/v1/models'
            headers = { Authorization: `Bearer ${api_key}` }
        } else if (provider === 'openrouter') {
            apiUrl = 'https://openrouter.ai/api/v1/models'
            headers = { Authorization: `Bearer ${api_key}` }
        } else if (provider === 'nvidia') {
            apiUrl = 'https://integrate.api.nvidia.com/v1/models'
            headers = { Authorization: `Bearer ${api_key}` }
        } else if (provider === 'openai') {
            apiUrl = 'https://api.openai.com/v1/models'
            headers = { Authorization: `Bearer ${api_key}` }
        } else if (provider === 'kimi' || provider === 'moonshot') {
            apiUrl = 'https://api.moonshot.ai/v1/models'
            headers = { Authorization: `Bearer ${api_key}` }
        } else if (provider === 'deepseek') {
            apiUrl = 'https://api.deepseek.com/v1/models'
            headers = { Authorization: `Bearer ${api_key}` }
        } else if (provider === 'ollama') {
            const ollamaUrl = process.env.OLLAMA_PROXY_URL || 'http://automais.ddns.net:11434'
            try {
                const r = await fetch(`${ollamaUrl}/api/tags`)
                const d = await r.json()
                const models = (d.models || []).map(m => ({ id: m.name, name: m.name }))
                return res.json({ success: true, models })
            } catch (e) {
                return res.status(500).json({ error: `Ollama offline: ${e.message}` })
            }
        } else {
            return res.status(400).json({ error: 'Provider inválido' })
        }

        const response = await fetch(apiUrl, { headers })
        const data = await response.json()

        if (data.error) throw new Error(data.error.message || 'Erro ao buscar modelos')

        let models = []
        if (provider === 'gemini') {
            models = (data.models || [])
                .filter(m => m.supportedGenerationMethods?.includes('generateContent'))
                .map(m => ({ id: m.name.replace('models/', ''), name: m.displayName || m.name.replace('models/', '') }))
        } else {
            models = (data.data || []).map(m => ({ id: m.id, name: m.id }))
        }

        res.json({ success: true, models })
    } catch (err) {
        res.status(500).json({ error: err.message })
    }
})

app.get('/groups/:tenantId', async (req, res) => {
    const session = sessions.get(req.params.tenantId)
    if (!session || session.status !== 'authenticated') return res.status(503).json({ success: false, error: 'Não conectado' })

    try {
        const groupMetadata = await session.sock.groupFetchAllParticipating()
        const groups = Object.values(groupMetadata).map(g => ({ id: g.id, subject: g.subject }))
        res.json({ success: true, groups })
    } catch (err) {
        res.status(500).json({ success: false, error: err.message })
    }
})

app.post('/send/:tenantId', async (req, res) => {
    const session = sessions.get(req.params.tenantId)
    if (!session || session.status !== 'authenticated') return res.status(503).json({ success: false, error: 'Não conectado' })

    try {
        const { number, message } = req.body
        if (!number || !message) return res.status(400).json({ error: 'number e message obrigatórios' })

        let jid = number
        if (!jid.includes('@')) {
            jid = (jid.includes('-') || jid.length > 15) ? `${jid}@g.us` : `${jid}@s.whatsapp.net`
        }

        await session.sock.sendMessage(jid, { text: message })
        res.json({ success: true })
    } catch (err) {
        res.status(500).json({ error: err.message })
    }
})

const PORT = process.env.WHATSAPP_PORT || 3001
app.listen(PORT, () => console.log(`🚀 RED IA WhatsApp Service v2.0 — porta ${PORT}`))
