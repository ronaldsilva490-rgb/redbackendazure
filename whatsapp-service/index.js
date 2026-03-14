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

const app = express()
app.use(cors())
app.use(express.json({ limit: '50mb' }))

const supabase = createClient(
    process.env.SUPABASE_URL || '',
    process.env.SUPABASE_SERVICE_KEY || process.env.SUPABASE_KEY || ''
)

const ADMIN_TENANT_ID = process.env.ADMIN_TENANT_ID || 'admin'
const sessions = new Map()
const conversationBuffers = new Map()
const MAX_BUFFER_MESSAGES = 5

// ── Controle de proatividade e atividade por conversa ──
const lastProactiveTime   = new Map() // key: tenantId_jid → timestamp
const groupActivityWindow = new Map() // key: tenantId_jid → [timestamps]
const PROACTIVE_COOLDOWN_MS   = 40000
const ACTIVITY_WINDOW_MS      = 120000 // janela de 2 min para medir atividade
const ACTIVE_GROUP_MSG_THRESH = 4      // msgs em 2min = grupo ativo

// ── Sticker pack (WebP base64 paths em disco) ──
const STICKER_DIR = path.join(__dirname, 'stickers')

// ── Cache de versão Baileys ──
let cachedBaileysVersion = null
async function getBaileysVersion() {
    if (cachedBaileysVersion) return cachedBaileysVersion
    try { cachedBaileysVersion = await fetchLatestBaileysVersion(); return cachedBaileysVersion }
    catch { return { version: [2, 3000, 1033846690], isLatest: true } }
}

// ── Schedule de mensagens agendadas ──
let scheduleInterval = null

process.on('uncaughtException',  (err) => console.error('❌ UncaughtException:',  err?.message))
process.on('unhandledRejection', (r)   => console.error('❌ UnhandledRejection:', r?.message || r))

// ══════════════════════════════════════════════════
// UTILITÁRIOS
// ══════════════════════════════════════════════════

function normalize(t) {
    return t ? t.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase() : ''
}

/** Registra atividade e retorna se o grupo está movimentado */
function trackGroupActivity(key) {
    const now = Date.now()
    if (!groupActivityWindow.has(key)) groupActivityWindow.set(key, [])
    const times = groupActivityWindow.get(key)
    times.push(now)
    // Remove timestamps fora da janela
    const cutoff = now - ACTIVITY_WINDOW_MS
    while (times.length && times[0] < cutoff) times.shift()
    return times.length >= ACTIVE_GROUP_MSG_THRESH
}

/** Verifica se a mensagem é recente (ignora mensagens velhas ao reiniciar) */
function isRecentMessage(msg) {
    const ts = (msg.messageTimestamp || 0) * 1000
    return (Date.now() - ts) < 90000 // ignora se tiver mais de 90s
}

// ══════════════════════════════════════════════════
// MÓDULO STT — Speech-to-Text
// ══════════════════════════════════════════════════
async function transcribeAudio(audioBuffer, mimeType, configs) {
    const sttCfg = configs.stt || {}
    const provider = sttCfg.provider || 'groq'
    const apiKey = sttCfg.api_key || configs.api_key || ''
    if (!apiKey) return null

    try {
        const tmpPath = path.join('/tmp', `audio_${Date.now()}.ogg`)
        fs.writeFileSync(tmpPath, audioBuffer)
        audioBuffer = null

        let apiUrl = 'https://api.groq.com/openai/v1/audio/transcriptions'
        const model = provider === 'openai' ? (sttCfg.model || 'whisper-1') : (sttCfg.model || 'whisper-large-v3-turbo')
        if (provider === 'openai') apiUrl = 'https://api.openai.com/v1/audio/transcriptions'

        const fileBytes = fs.readFileSync(tmpPath)
        const blob = new Blob([fileBytes], { type: mimeType || 'audio/ogg' })
        const form = new globalThis.FormData()
        form.append('file', blob, 'audio.ogg')
        form.append('model', model)
        form.append('language', 'pt')
        form.append('response_format', 'text')

        const resp = await fetch(apiUrl, { method: 'POST', headers: { Authorization: `Bearer ${apiKey}` }, body: form })
        try { fs.unlinkSync(tmpPath) } catch (_) {}

        if (!resp.ok) { console.error('[STT] Erro:', await resp.text()); return null }
        const text = await resp.text()
        console.log(`[STT] ✅ "${text.trim().substring(0, 80)}"`)
        return text.trim()
    } catch (err) {
        console.error('[STT] Exceção:', err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// MÓDULO VISÃO — Análise de Imagem
// ══════════════════════════════════════════════════
async function analyzeImage(imageBuffer, caption, configs) {
    const visionCfg = configs.vision || {}
    const provider = visionCfg.provider || configs.ai_provider || 'gemini'
    const apiKey = visionCfg.api_key || configs.api_key || ''
    const model = visionCfg.model || 'gemini-2.0-flash'
    if (!apiKey) return null

    try {
        let imgData = imageBuffer
        if (imageBuffer.length > 800_000) {
            try {
                const sharp = require('sharp')
                imgData = await sharp(imageBuffer).resize(800, 800, { fit: 'inside', withoutEnlargement: true }).jpeg({ quality: 75 }).toBuffer()
            } catch (_) {}
        }
        const base64 = imgData.toString('base64')
        imgData = null; imageBuffer = null
        const question = caption ? `Caption: "${caption}". Descreva o que vê e comente.` : 'Descreva detalhadamente.'

        if (provider === 'gemini') {
            const genAI = new GoogleGenerativeAI(apiKey)
            const mdl = genAI.getGenerativeModel({ model })
            const result = await mdl.generateContent([{ inlineData: { mimeType: 'image/jpeg', data: base64 } }, question])
            return result.response.text()
        }

        let apiUrl = 'https://openrouter.ai/api/v1/chat/completions'
        if (provider === 'openai') apiUrl = 'https://api.openai.com/v1/chat/completions'
        if (provider === 'nvidia') apiUrl = 'https://integrate.api.nvidia.com/v1/chat/completions'

        const resp = await fetch(apiUrl, {
            method: 'POST',
            headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, messages: [{ role: 'user', content: [{ type: 'image_url', image_url: { url: `data:image/jpeg;base64,${base64}` } }, { type: 'text', text: question }] }] })
        })
        const data = await resp.json()
        return data.choices?.[0]?.message?.content || null
    } catch (err) {
        console.error('[VISION] Exceção:', err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// MÓDULO TTS — Edge TTS com SSML e prosódia real
// ══════════════════════════════════════════════════

const VALID_EDGE_VOICES_PTBR = [
    'pt-BR-FranciscaNeural',
    'pt-BR-AntonioNeural',
    'pt-BR-ThalitaMultilingualNeural',
]

const EDGE_VOICE_STYLE = {
    'pt-BR-FranciscaNeural':           { rate: '-5%', pitch: '+0Hz' },
    'pt-BR-AntonioNeural':             { rate: '-5%', pitch: '+0Hz' },
    'pt-BR-ThalitaMultilingualNeural': { rate:  '0%', pitch: '+0Hz' },
}

function cleanTextForTTS(text) {
    if (!text) return ''
    let c = text
    // Remove risadas (kkk, haha, rsrs, lol…)
    c = c.replace(/\b(k{2,}|ha(ha)+|rs(rs)*|lol+|hu(hu)+|he(he)+|ih+|ui+)\b/gi, '')
    // Remove emojis Unicode
    c = c.replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}\u{2300}-\u{23FF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{1FA00}-\u{1FAFF}]/gu, '')
    c = c.replace(/[\uFE0F\u200D\u20E3]/g, '')
    // Remove markdown WhatsApp
    c = c.replace(/[*_~`]/g, '')
    // URLs
    c = c.replace(/https?:\/\/\S+/g, '')
    // Colapsa espaços
    c = c.replace(/\s{2,}/g, ' ').trim()
    return c.substring(0, 500)
}

/** Converte pontuação em pausas SSML reais */
function textToSSML(text, voiceId, rate, pitch, volume) {
    const esc = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        // Ponto final → pausa longa
        .replace(/\.\s+/g, '. <break time="400ms"/> ')
        .replace(/\.\s*$/g, '. <break time="400ms"/>')
        // Ponto de exclamação → pausa média + ênfase
        .replace(/!\s*/g, '! <break time="350ms"/> ')
        // Interrogação → pausa média
        .replace(/\?\s*/g, '? <break time="350ms"/> ')
        // Reticências → pausa dramática
        .replace(/\.\.\.\s*/g, '<break time="600ms"/> ')
        // Vírgula → pausa curta
        .replace(/,\s*/g, ', <break time="150ms"/> ')
        // Ponto e vírgula → pausa média-curta
        .replace(/;\s*/g, '; <break time="250ms"/> ')
        // Travessão → pausa
        .replace(/—\s*/g, '<break time="300ms"/> ')

    return `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='pt-BR'><voice name='${voiceId}'><prosody rate='${rate}' pitch='${pitch}' volume='${volume}'>${esc}</prosody></voice></speak>`
}

async function generateAudio(text, configs) {
    const ttsCfg = configs.tts || {}
    if (!(ttsCfg.enabled === true || ttsCfg.enabled === 'true')) return null

    const provider = ttsCfg.provider || 'edge'
    const cleanText = cleanTextForTTS(text)
    if (!cleanText) return null

    try {
        const { execFile } = require('child_process')
        const { promisify } = require('util')
        const execFileAsync = promisify(execFile)

        const tmpMp3 = path.join('/tmp', `tts_${Date.now()}.mp3`)
        const tmpWav = path.join('/tmp', `tts_${Date.now()}.wav`)
        const tmpOgg = path.join('/tmp', `tts_${Date.now() + 1}.ogg`)
        let generated = false

        if (provider === 'edge') {
            let voiceId = ttsCfg.voice_id || 'pt-BR-FranciscaNeural'
            if (!VALID_EDGE_VOICES_PTBR.includes(voiceId)) {
                console.warn(`[TTS] Voz "${voiceId}" obsoleta → pt-BR-FranciscaNeural`)
                voiceId = 'pt-BR-FranciscaNeural'
            }
            const voiceDefault = EDGE_VOICE_STYLE[voiceId] || { rate: '-5%', pitch: '+0Hz' }
            const ttsRate   = ttsCfg.rate   || voiceDefault.rate
            const ttsPitch  = ttsCfg.pitch  || voiceDefault.pitch
            const ttsVolume = ttsCfg.volume || '+0%'

            console.log(`[TTS] Edge-TTS → ${voiceId} | rate:${ttsRate} pitch:${ttsPitch} vol:${ttsVolume}`)

            // Gera SSML com pausas reais baseadas na pontuação
            const ssmlText = textToSSML(cleanText, voiceId, ttsRate, ttsPitch, ttsVolume)

            const pyScript = `
import asyncio, sys, edge_tts
async def run():
    c = edge_tts.Communicate(
        text=sys.argv[1],
        voice=sys.argv[2],
        rate=sys.argv[3],
        pitch=sys.argv[4],
        volume=sys.argv[5]
    )
    await c.save(sys.argv[6])
asyncio.run(run())`.trim()

            const pyFile = path.join('/tmp', `tts_py_${Date.now()}.py`)
            fs.writeFileSync(pyFile, pyScript, 'utf8')

            try {
                await execFileAsync('python3', [
                    pyFile, cleanText, voiceId, ttsRate, ttsPitch, ttsVolume, tmpMp3
                ], { timeout: 25000 })
            } finally {
                try { fs.unlinkSync(pyFile) } catch (_) {}
            }

            if (fs.existsSync(tmpMp3)) {
                await execFileAsync('ffmpeg', ['-y', '-i', tmpMp3, '-c:a', 'libopus', '-b:a', '32k', '-vbr', 'on', tmpOgg], { timeout: 15000 })
                try { fs.unlinkSync(tmpMp3) } catch (_) {}
                generated = true
            }
        }

        if (provider === 'espeak') {
            try {
                await execFileAsync('espeak-ng', ['-v', 'pt-br', '-s', '155', '-p', '60', '-a', '180', '-w', tmpWav, cleanText], { timeout: 15000 })
                if (fs.existsSync(tmpWav)) {
                    await execFileAsync('ffmpeg', ['-y', '-i', tmpWav, '-c:a', 'libopus', '-b:a', '24k', '-vbr', 'on', tmpOgg], { timeout: 15000 })
                    try { fs.unlinkSync(tmpWav) } catch (_) {}
                    generated = true
                }
            } catch (err) { console.error('[TTS] espeak falhou:', err.message); return null }
        }

        if (!generated || !fs.existsSync(tmpOgg)) return null
        const audioBuffer = fs.readFileSync(tmpOgg)
        try { fs.unlinkSync(tmpOgg) } catch (_) {}
        console.log(`[TTS] ✅ ${audioBuffer.length} bytes via ${provider}`)
        return audioBuffer
    } catch (err) {
        console.error('[TTS] Exceção:', err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// REAÇÕES AUTOMÁTICAS
// ══════════════════════════════════════════════════
const REACTION_MAP = {
    // Gatilhos → emojis possíveis
    engraçado:  ['😂', '😆', '💀', '🤣'],
    positivo:   ['❤️', '🔥', '👏', '💪'],
    surpresa:   ['😮', '👀', '🤯', '😱'],
    concordo:   ['👍', '💯', '✅', '👌'],
    carinho:    ['❤️', '🥰', '💕', '😘'],
    triste:     ['😢', '🫂', '💙', '😔'],
    neutro:     ['👀', '🤔', '💬'],
}

function pickReaction(vibe, text) {
    const t = (text || '').toLowerCase()
    if (/kkk|haha|rsrs|lol|😂|😆/.test(t))                    return pick(REACTION_MAP.engraçado)
    if (/boa|parab|feli|top|show|incrív|maneiro|demais/.test(t)) return pick(REACTION_MAP.positivo)
    if (/serio|sério|meus deus|que isso|caramba|oxe/.test(t))    return pick(REACTION_MAP.surpresa)
    if (/sim|claro|exato|concordo|verdade|isso aí/.test(t))      return pick(REACTION_MAP.concordo)
    if (/te amo|saudade|falta|amor|beijo/.test(t))               return pick(REACTION_MAP.carinho)
    if (/triste|choran|difícil|ruim|não consigo/.test(t))        return pick(REACTION_MAP.triste)
    if (/zoeira|piada|meme|kkk/.test(vibe?.toLowerCase() || '')) return pick(REACTION_MAP.engraçado)
    return Math.random() < 0.3 ? pick(REACTION_MAP.neutro) : null
}

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)] }

async function sendReaction(sock, msg, emoji) {
    if (!emoji) return
    try {
        await sock.sendMessage(msg.key.remoteJid, {
            react: { text: emoji, key: msg.key }
        })
    } catch (e) {
        console.warn('[REACT] Falhou:', e.message)
    }
}

// ══════════════════════════════════════════════════
// STICKERS
// ══════════════════════════════════════════════════
async function sendSticker(sock, remoteJid, mood) {
    if (!fs.existsSync(STICKER_DIR)) return
    try {
        const files = fs.readdirSync(STICKER_DIR).filter(f => {
            const lf = f.toLowerCase()
            if (mood === 'happy')   return lf.includes('happy') || lf.includes('feliz') || lf.includes('good')
            if (mood === 'love')    return lf.includes('love') || lf.includes('amor') || lf.includes('heart')
            if (mood === 'laugh')   return lf.includes('laugh') || lf.includes('rs') || lf.includes('lol')
            if (mood === 'wow')     return lf.includes('wow') || lf.includes('surpres') || lf.includes('omg')
            return true
        })
        if (!files.length) return
        const file = files[Math.floor(Math.random() * files.length)]
        const sticker = fs.readFileSync(path.join(STICKER_DIR, file))
        await sock.sendMessage(remoteJid, { sticker })
        console.log(`[STICKER] 🎭 Enviou ${file} (mood: ${mood})`)
    } catch (e) {
        console.warn('[STICKER] Falhou:', e.message)
    }
}

// ══════════════════════════════════════════════════
// ENVIO INTELIGENTE — typing/recording + fallback texto
// ══════════════════════════════════════════════════
async function humanDelay(ms) { return new Promise(r => setTimeout(r, ms)) }

async function sendSmartResponse(sock, remoteJid, text, quotedMsg, configs, extraOpts = {}) {
    const ttsCfg = configs.tts || {}
    const ttsEnabled = ttsCfg.enabled === true || ttsCfg.enabled === 'true'
    const shouldSendAudio = ttsEnabled && Math.random() < (parseFloat(ttsCfg.audio_probability) || 0.3)

    if (shouldSendAudio && text.length < 500) {
        try {
            await sock.sendPresenceUpdate('recording', remoteJid)
            const audioBuffer = await generateAudio(text, configs)
            if (audioBuffer) {
                await humanDelay(800 + Math.random() * 1200)
                await sock.sendPresenceUpdate('available', remoteJid)
                await sock.sendMessage(remoteJid, { audio: audioBuffer, mimetype: 'audio/ogg; codecs=opus', ptt: true }, { quoted: quotedMsg })
                console.log(`[SEND] ✅ PTT → ${remoteJid}`)
                return
            }
            await sock.sendPresenceUpdate('available', remoteJid)
        } catch (_) {}
    }

    // Typing indicator proporcional ao texto
    try {
        await sock.sendPresenceUpdate('composing', remoteJid)
        await humanDelay(Math.min(4000, Math.max(800, text.length * 30)))
        await sock.sendPresenceUpdate('available', remoteJid)
    } catch (_) {}

    await sock.sendMessage(remoteJid, { text }, { quoted: quotedMsg })
}

// ══════════════════════════════════════════════════
// CORE IA — Geração de Resposta
// ══════════════════════════════════════════════════
async function getAIResponse(prompt, configs, overrideSystemPrompt = null) {
    const chatCfg = configs.chat || {}
    const provider = chatCfg.provider || configs.ai_provider || 'gemini'
    const apiKey = chatCfg.api_key || configs.api_key || ''
    const model = chatCfg.model || configs.model || ''
    const systemPrompt = overrideSystemPrompt || chatCfg.system_prompt || configs.system_prompt || 'Você é um assistente.'

    if (!apiKey || !model) { console.warn(`[AI] Config incompleta (${provider})`); return null }

    try {
        if (provider === 'gemini') {
            const genAI = new GoogleGenerativeAI(apiKey)
            const mdl = genAI.getGenerativeModel({ model, systemInstruction: systemPrompt })
            const result = await mdl.generateContent(prompt)
            return result.response.text()
        }

        let apiUrl = ''
        if (provider === 'groq')       apiUrl = 'https://api.groq.com/openai/v1/chat/completions'
        else if (provider === 'openrouter') apiUrl = 'https://openrouter.ai/api/v1/chat/completions'
        else if (provider === 'nvidia')    apiUrl = 'https://integrate.api.nvidia.com/v1/chat/completions'
        else if (provider === 'openai')    apiUrl = 'https://api.openai.com/v1/chat/completions'
        else if (provider === 'kimi' || provider === 'moonshot') apiUrl = 'https://api.moonshot.ai/v1/chat/completions'
        else if (provider === 'deepseek')  apiUrl = 'https://api.deepseek.com/v1/chat/completions'
        else if (provider === 'ollama') {
            const ollamaUrl = process.env.OLLAMA_PROXY_URL || 'http://localhost:11434'
            apiUrl = `${ollamaUrl}/v1/chat/completions`
        } else return null

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
                messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: prompt }],
                max_tokens: 1024,
                temperature: 0.88
            })
        })
        const data = await resp.json()
        if (data.error) { console.error(`[AI] Erro ${provider}:`, data.error); return null }
        return data.choices?.[0]?.message?.content || null
    } catch (err) {
        console.error(`[AI] Exceção (${provider}):`, err.message)
        return null
    }
}

// ══════════════════════════════════════════════════
// MEMÓRIA DE LONGO PRAZO — Salva e busca fatos por contato
// ══════════════════════════════════════════════════
async function saveMemoryFact(tenantId, contactJid, fact, category = 'geral') {
    try {
        await supabase.from('whatsapp_long_term_memory').insert({
            tenant_id: tenantId,
            contact_jid: contactJid,
            fact,
            category,
            created_at: new Date()
        })
    } catch (_) {}
}

async function getContactMemory(tenantId, contactJid, limit = 10) {
    try {
        const { data } = await supabase
            .from('whatsapp_long_term_memory')
            .select('fact, category, created_at')
            .eq('tenant_id', tenantId)
            .eq('contact_jid', contactJid)
            .order('created_at', { ascending: false })
            .limit(limit)
        if (!data?.length) return ''
        return '\n[MEMÓRIA DE LONGO PRAZO:\n' + data.map(d => `• ${d.fact}`).join('\n') + ']'
    } catch (_) { return '' }
}

// ══════════════════════════════════════════════════
// APRENDIZADO CONTÍNUO + PROATIVIDADE INTELIGENTE
// ══════════════════════════════════════════════════
async function learnFromConversation(tenantId, conversationId, newMessages, aiConfigs, isActiveGroup = false) {
    const chatCfg = aiConfigs.chat || {}
    const apiKey = chatCfg.api_key || aiConfigs.api_key || ''
    if (!apiKey) return

    console.log(`[LEARN] 🧠 ${conversationId} (ativo: ${isActiveGroup})`)

    try {
        const { data: currentContext } = await supabase
            .from('whatsapp_conversation_contexts')
            .select('*')
            .eq('tenant_id', tenantId)
            .eq('conversation_id', conversationId)
            .single()

        const oldSummary = currentContext?.summary || 'Sem histórico anterior.'
        const transcript = newMessages.map(m => `${m.author}: ${m.text}`).join('\n')
        const chatPersonality = chatCfg.system_prompt || 'Você é um membro humano do grupo.'

        const proactiveInstruction = isActiveGroup
            ? `O grupo está MOVIMENTADO agora. A IA DEVE ter algo a dizer se o assunto for minimamente interessante. Seja mais ousada em participar.`
            : `Só entre na conversa se houver algo genuinamente interessante. Prefira silêncio a interrupções sem valor.`

        const prompt = `Você é um especialista em análise social e comportamento humano em grupos de WhatsApp.

PERSONALIDADE DA IA: "${chatPersonality}"
RESUMO ANTERIOR: "${oldSummary}"
NOVAS MENSAGENS:
${transcript}

${proactiveInstruction}

Retorne APENAS JSON puro com:
1. "summary": Resumo completo atualizado.
2. "vibe": Humor atual em pt-BR (ex: "Zoeira total", "Animado", "Cotidiano").
3. "group_type": Tipo do grupo.
4. "daily_topics": Tópicos do momento.
5. "style": Gírias e expressões usadas.
6. "profiles": [{ "jid", "name", "nicknames": [], "personality", "style_note", "memory_facts": [] }]
   — memory_facts: fatos IMPORTANTES sobre a pessoa para lembrar a longo prazo (viagem, relacionamento, trabalho, problema de saúde, etc). Deixe vazio se nada relevante.
7. "proactive_thought": Fala CURTA e NATURAL da IA como humano do grupo. Use gírias do "style". Máx 2 frases. Vazio se nada relevante.
8. "proactive_urgency": 0-10. 0=silêncio, 6+=vale participar, 9+=imperdível.
9. "proactive_trigger": Gatilho ("pergunta aberta", "piada", "polêmica", "celebração", "alguém mencionou a IA indiretamente"). Vazio se nenhum.
10. "proactive_reaction_mood": Mood para sticker/reação opcional ("happy","love","laugh","wow","none").
11. "intent": Intenção detectada na última mensagem ("pergunta_preco", "pergunta_horario", "elogio", "reclamacao", "conversa", "pedido_ajuda", "outro").
12. "handoff_needed": true se a conversa exige atenção humana (reclamação séria, pergunta técnica impossível, pedido urgente fora do escopo).
13. "context_for_next_response": Frase curta do que a IA precisa saber para a próxima resposta.

Retorne APENAS o JSON puro.`

        const proactiveCfgForAI = aiConfigs.proactive || {}
        const learningChatCfg = (proactiveCfgForAI.provider && proactiveCfgForAI.api_key && proactiveCfgForAI.model)
            ? { provider: proactiveCfgForAI.provider, api_key: proactiveCfgForAI.api_key, model: proactiveCfgForAI.model, system_prompt: chatCfg.system_prompt }
            : { ...chatCfg }
        const analysisCfg = { ...aiConfigs, chat: learningChatCfg }
        const aiResponse = await getAIResponse(prompt, analysisCfg, 'Analista de comportamento. Responda apenas com JSON puro.')

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

        // Atualiza perfis + salva memória de longo prazo
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

                // Salva fatos de memória longa
                if (p.memory_facts?.length) {
                    for (const fact of p.memory_facts) {
                        if (fact && fact.length > 5) {
                            await saveMemoryFact(tenantId, p.jid, fact)
                        }
                    }
                }
            }
        }

        // Handoff humano
        if (result.handoff_needed) {
            console.log(`[HANDOFF] ⚠️ Atenção humana necessária em ${conversationId}`)
            try {
                await supabase.from('whatsapp_handoff_queue').upsert({
                    tenant_id: tenantId,
                    conversation_id: conversationId,
                    reason: result.intent || 'Detectado pela IA',
                    status: 'pending',
                    created_at: new Date()
                }, { onConflict: 'tenant_id, conversation_id' })
            } catch (_) {}
        }

        // ── Intervenção Proativa ──
        const urgency = parseFloat(result.proactive_urgency) || 0
        const proactiveThought = (result.proactive_thought || '').trim()
        const trigger = result.proactive_trigger || ''
        const reactionMood = result.proactive_reaction_mood || 'none'

        const lastProactiveKey = `${tenantId}_${conversationId}`
        const timeSinceLastProactive = Date.now() - (lastProactiveTime.get(lastProactiveKey) || 0)

        const proactiveCfg = aiConfigs.proactive || {}
        const proactiveEnabled = proactiveCfg.enabled !== false && proactiveCfg.enabled !== 'false'
        const frequency = parseFloat(proactiveCfg.frequency || 0.15)

        // Cooldown adaptativo
        const adaptiveCooldown = urgency >= 9 ? 8000 : urgency >= 7 ? 18000 : urgency >= 5 ? 28000 : PROACTIVE_COOLDOWN_MS

        // Boost de frequência se grupo ativo
        const effectiveFrequency = isActiveGroup ? Math.min(frequency * 2.5, 0.7) : frequency

        const randomRoll = Math.random()
        const shouldParticipate = proactiveEnabled
            && proactiveThought.length > 3
            && timeSinceLastProactive > adaptiveCooldown
            && (
                urgency >= 9
                || (urgency >= 7 && randomRoll < 0.88)
                || (urgency >= 5 && randomRoll < effectiveFrequency * 1.5)
                || randomRoll < effectiveFrequency
            )

        if (shouldParticipate) {
            const session = sessions.get(tenantId)
            if (session?.sock && session.status === 'authenticated') {
                console.log(`[PROATIVO] 🤖 urgência:${urgency} trigger:"${trigger}" ativo:${isActiveGroup}`)
                lastProactiveTime.set(lastProactiveKey, Date.now())

                const minDelay = urgency >= 8 ? 1200 : urgency >= 5 ? 2500 : 4000
                const maxDelay = urgency >= 8 ? 3500 : urgency >= 5 ? 7000 : 14000
                const delay = minDelay + Math.random() * (maxDelay - minDelay)

                setTimeout(async () => {
                    try {
                        // Às vezes envia sticker antes de falar
                        if (reactionMood !== 'none' && Math.random() < 0.25) {
                            await sendSticker(session.sock, conversationId, reactionMood)
                            await humanDelay(800 + Math.random() * 1200)
                        }
                        await sendSmartResponse(session.sock, conversationId, proactiveThought, null, aiConfigs)
                    } catch (e) {
                        console.error('[PROATIVO] Erro:', e.message)
                    }
                }, delay)
            }
        }

        console.log(`[LEARN] ✅ ${conversationId} | Vibe: ${result.vibe} | Intent: ${result.intent}`)
    } catch (err) {
        console.error(`[LEARN] ❌ Erro:`, err.message)
    }
}

// ══════════════════════════════════════════════════
// DETECÇÃO DE INTENÇÃO — Respostas rápidas sem IA
// ══════════════════════════════════════════════════
function detectSimpleIntent(text) {
    const t = normalize(text)
    if (/qual.*(preco|valor|custa|quanto)/.test(t) || /preco|valor custa|quanto e/.test(t)) return 'pergunta_preco'
    if (/que horas|horario|abre|fecha|funcionamento/.test(t)) return 'pergunta_horario'
    if (/onde fica|endereco|localizacao|como chegar/.test(t)) return 'pergunta_endereco'
    if (/whatsapp|zap|telefone|contato|numero/.test(t)) return 'pergunta_contato'
    return null
}

// ══════════════════════════════════════════════════
// CONFIGS POR GRUPO — personalidade específica
// ══════════════════════════════════════════════════
async function getGroupPersonality(tenantId, groupJid) {
    try {
        const { data } = await supabase
            .from('whatsapp_group_configs')
            .select('system_prompt, personality_name, enabled')
            .eq('tenant_id', tenantId)
            .eq('group_jid', groupJid)
            .single()
        return data || null
    } catch (_) { return null }
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

            configData = {
                chat: {
                    provider: configs.chat_provider || provider,
                    api_key: configs[`${configs.chat_provider || provider}_api_key`] || configs[`${provider}_api_key`] || process.env.GEMINI_API_KEY || '',
                    model: configs.chat_model || configs[`${provider}_model`] || 'gemini-2.0-flash',
                    system_prompt: configs.chat_system_prompt || configs[`${provider}_system_prompt`] || 'Você é o assistente RED.IA.'
                },
                stt: {
                    provider: configs.stt_provider || 'groq',
                    api_key: configs.stt_api_key || configs.groq_api_key || '',
                    model: configs.stt_model || 'whisper-large-v3-turbo',
                    enabled: configs.stt_enabled !== 'false'
                },
                vision: {
                    provider: configs.vision_provider || 'gemini',
                    api_key: configs.vision_api_key || configs.gemini_api_key || process.env.GEMINI_API_KEY || '',
                    model: configs.vision_model || 'gemini-2.0-flash',
                    enabled: configs.vision_enabled !== 'false'
                },
                tts: {
                    provider: configs.tts_provider || 'edge',
                    api_key: configs.tts_api_key || '',
                    model: configs.tts_model || '',
                    voice_id: configs.tts_voice_id || 'pt-BR-FranciscaNeural',
                    enabled: configs.tts_enabled === 'true',
                    audio_probability: parseFloat(configs.tts_audio_probability) || 0.3,
                    rate: configs.tts_rate || '-5%',
                    pitch: configs.tts_pitch || '+0Hz',
                    volume: configs.tts_volume || '+0%'
                },
                learning: {
                    provider: configs.learning_provider || configs.chat_provider || provider,
                    api_key: configs.learning_api_key || configs[`${provider}_api_key`] || '',
                    model: configs.learning_model || configs[`${provider}_model`] || 'gemini-2.0-flash',
                    enabled: configs.learning_enabled !== 'false'
                },
                proactive: {
                    enabled: configs.proactive_enabled !== 'false',
                    frequency: parseFloat(configs.proactive_frequency) || 0.15,
                    provider: configs.proactive_provider || configs.chat_provider || provider,
                    api_key: configs.proactive_api_key || configs[`${configs.proactive_provider || configs.chat_provider || provider}_api_key`] || '',
                    model: configs.proactive_model || configs.chat_model || ''
                },
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
                    system_prompt: d.system_prompt || 'Você é o assistente virtual.'
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
                    model: d.tts_model || '',
                    voice_id: d.tts_voice_id || 'pt-BR-FranciscaNeural',
                    enabled: d.tts_enabled === true || d.tts_enabled === 'true',
                    audio_probability: parseFloat(d.tts_audio_probability) || 0.3,
                    rate: d.tts_rate || '-5%',
                    pitch: d.tts_pitch || '+0Hz',
                    volume: d.tts_volume || '+0%'
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
            console.log(`✅ Configs [${tenantId}] Chat: ${configData.chat?.provider}/${configData.chat?.model}`)
        }
    } catch (err) {
        console.error(`Erro ao carregar configs [${tenantId}]:`, err?.message)
        const session = sessions.get(tenantId)
        if (session) session.aiConfigs = { chat: { provider: 'gemini', api_key: process.env.GEMINI_API_KEY || '', model: 'gemini-2.0-flash', system_prompt: 'Você é um assistente.' }, stt: { enabled: false }, vision: { enabled: false }, tts: { enabled: false }, learning: { enabled: false }, proactive: { enabled: false }, ai_bot_enabled: false }
    }
}

// ══════════════════════════════════════════════════
// RESOLUÇÃO DE NOMES
// ══════════════════════════════════════════════════
async function resolveNames(text, tenantId, sock) {
    if (!text) return text
    const jidRegex = /(@\d+|@[\w.-]+(@g\.us|@s\.whatsapp\.net|@lid))/g
    const matches = text.match(jidRegex) || []
    let resolvedText = text
    for (const jid of matches) {
        let cleanJid = jid.startsWith('@') ? jid.substring(1) : jid
        if (!cleanJid.includes('@')) cleanJid = cleanJid + (cleanJid.includes('-') ? '@g.us' : '@s.whatsapp.net')
        try {
            let name = null
            if (cleanJid.endsWith('@g.us')) {
                const meta = await sock.groupMetadata(cleanJid).catch(() => null)
                name = meta?.subject
            } else {
                const { data: contact } = await supabase.from('whatsapp_contact_profiles')
                    .select('full_name, nickname').eq('tenant_id', tenantId).eq('contact_id', cleanJid).single()
                name = contact?.nickname || contact?.full_name
            }
            if (name) resolvedText = resolvedText.replace(jid, `@${name}`)
        } catch (_) {}
    }
    return resolvedText
}

// ══════════════════════════════════════════════════
// CONTEXTO DA EMPRESA
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
    } catch (err) { return '' }
}

// ══════════════════════════════════════════════════
// MENSAGENS AGENDADAS
// ══════════════════════════════════════════════════
async function runScheduledMessages() {
    try {
        const now = new Date()
        const currentTime = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`
        const currentDay = ['dom','seg','ter','qua','qui','sex','sab'][now.getDay()]

        const { data: schedules } = await supabase
            .from('whatsapp_schedules')
            .select('*')
            .eq('enabled', true)
            .eq('send_time', currentTime)

        if (!schedules?.length) return

        for (const sched of schedules) {
            // Verifica dia da semana
            if (sched.days && !sched.days.includes(currentDay)) continue

            const session = sessions.get(sched.tenant_id || ADMIN_TENANT_ID)
            if (!session?.sock || session.status !== 'authenticated') continue

            // Evita duplicata no mesmo minuto
            const schedKey = `sched_${sched.id}_${currentTime}`
            if (lastProactiveTime.get(schedKey)) continue
            lastProactiveTime.set(schedKey, Date.now())
            setTimeout(() => lastProactiveTime.delete(schedKey), 70000)

            try {
                let jid = sched.target_jid
                if (!jid.includes('@')) jid = jid.includes('-') ? `${jid}@g.us` : `${jid}@s.whatsapp.net`

                let message = sched.message
                // Se mensagem dinâmica, pede à IA
                if (sched.ai_generated && session.aiConfigs) {
                    const aiMsg = await getAIResponse(
                        `Escreva uma mensagem curta e natural para: "${sched.message}". Contexto: grupo do WhatsApp, tom descontraído.`,
                        session.aiConfigs
                    )
                    if (aiMsg) message = aiMsg
                }

                await sendSmartResponse(session.sock, jid, message, null, session.aiConfigs || {})
                console.log(`[SCHEDULE] ✅ "${sched.message?.substring(0,40)}" → ${jid}`)
            } catch (e) {
                console.error('[SCHEDULE] Erro:', e.message)
            }
        }
    } catch (err) {
        console.error('[SCHEDULE] Exceção:', err.message)
    }
}

// ══════════════════════════════════════════════════
// CONEXÃO WHATSAPP
// ══════════════════════════════════════════════════
async function connectToWhatsApp(tenantId, forceReset = false) {
    console.log(`[WA] Conectando tenant: ${tenantId}${forceReset ? ' (RESET)' : ''}`)
    const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)

    if (forceReset && fs.existsSync(authPath)) {
        fs.rmSync(authPath, { recursive: true, force: true })
    }
    if (!fs.existsSync(authPath)) fs.mkdirSync(authPath, { recursive: true })

    const { state, saveCreds } = await useMultiFileAuthState(authPath)
    const { version } = await getBaileysVersion()

    const sock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,
        browser: Browsers.macOS('Desktop'),
        logger: pino({ level: 'warn' }),
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 25000
    })

    const session = { sock, aiConfigs: null, lastQr: null, status: 'connecting' }
    sessions.set(tenantId, session)
    await loadTenantAIConfigs(tenantId)

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            session.lastQr = await QRCode.toDataURL(qr)
            session.status = 'qrcode'
            try { await supabase.from('whatsapp_sessions').upsert({ tenant_id: tenantId, status: 'qrcode', qr: session.lastQr, updated_at: new Date() }, { onConflict: 'tenant_id' }) } catch (_) {}
        }

        if (connection === 'close') {
            const statusCode = lastDisconnect?.error?.output?.statusCode
            session.status = 'disconnected'
            session.lastQr = null
            if (statusCode === DisconnectReason.loggedOut || statusCode === 428) {
                sessions.delete(tenantId)
                try { await supabase.from('whatsapp_sessions').delete().eq('tenant_id', tenantId) } catch (_) {}
                if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true })
                if (statusCode === 428) setTimeout(() => connectToWhatsApp(tenantId, false), 3000)
            } else {
                setTimeout(() => connectToWhatsApp(tenantId, false), 2000)
            }
        } else if (connection === 'open') {
            session.status = 'authenticated'
            session.lastQr = null
            try { await supabase.from('whatsapp_sessions').upsert({ tenant_id: tenantId, status: 'authenticated', phone: sock.user.id, qr: null, updated_at: new Date() }, { onConflict: 'tenant_id' }) } catch (_) {}
            console.log(`✅ Tenant ${tenantId} conectado!`)
        }
    })

    sock.ev.on('creds.update', saveCreds)

    // ══════════════════════════════════════════════
    // LISTENER PRINCIPAL DE MENSAGENS
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

            // ── Ignora mensagens antigas (evita processamento no reinício) ──
            if (!isRecentMessage(msg)) {
                console.log(`[SKIP] Mensagem antiga ignorada (${new Date((msg.messageTimestamp || 0) * 1000).toLocaleTimeString()})`)
                continue
            }

            const remoteJid = msg.key.remoteJid
            const isGroup = remoteJid.endsWith('@g.us')
            const isPV = !isGroup

            const msgType = Object.keys(msg.message)[0]
            let textContent = ''
            let mediaContent = null

            if (msgType === 'conversation')          textContent = msg.message.conversation
            else if (msgType === 'extendedTextMessage') textContent = msg.message.extendedTextMessage.text
            else if (msgType === 'buttonsResponseMessage') textContent = msg.message.buttonsResponseMessage.selectedButtonId
            else if (msgType === 'listResponseMessage')   textContent = msg.message.listResponseMessage.singleSelectReply.selectedRowId
            else if (msg.message[msgType]?.text)    textContent = msg.message[msgType].text
            else if (msg.message[msgType]?.caption) textContent = msg.message[msgType].caption

            const isAudio  = msgType === 'audioMessage'
            const isImage  = msgType === 'imageMessage' || (msgType === 'viewOnceMessageV2' && msg.message.viewOnceMessageV2?.message?.imageMessage)
            const isSticker = msgType === 'stickerMessage'

            // ── Detecção de menção / resposta ao bot ──
            const contextInfo = msg.message?.extendedTextMessage?.contextInfo
                || msg.message?.imageMessage?.contextInfo
                || msg.message?.audioMessage?.contextInfo
                || msg.message?.ephemeralMessage?.message?.extendedTextMessage?.contextInfo
                || msg.message?.viewOnceMessageV2?.message?.imageMessage?.contextInfo

            const isMentioned = !!contextInfo?.mentionedJid?.some(jid =>
                jid.includes(botId) || (botLidShort && jid.includes(botLidShort))
            )
            const isReplyToMe = !!(
                contextInfo?.participant?.includes(botId) ||
                (botLidShort && contextInfo?.participant?.includes(botLidShort))
            )

            const configs    = session.aiConfigs || {}
            const isBotEnabled = String(configs.ai_bot_enabled) === 'true'

            // ── Keyword ──
            const keyword = (configs.ai_prefix || '').trim()
            const containsKeyword = Boolean(keyword && normalize(textContent).includes(normalize(keyword)))

            // ── Nome da IA no texto (referência indireta) ──
            const botName = configs.chat?.system_prompt?.match(/meu nome é (\w+)/i)?.[1] || 'ia'
            const mentionedByName = botName.length > 2 && normalize(textContent).includes(normalize(botName))

            // ── Buffer de atividade (para detectar grupo movimentado) ──
            const author    = msg.pushName || remoteJid.split('@')[0]
            const authorJid = msg.key.participant || remoteJid
            const bufferKey = `${tenantId}_${remoteJid}`
            const isActiveGroup = isGroup && trackGroupActivity(bufferKey)

            if (!conversationBuffers.has(bufferKey)) conversationBuffers.set(bufferKey, { tenantId, messages: [] })
            const buffer = conversationBuffers.get(bufferKey)

            const learningEnabled = configs.learning?.enabled !== false
            const sttEnabled = configs.stt?.enabled !== false

            let bufferText = textContent.trim()

            // ── Processa mídia para o buffer ──
            if ((isAudio || isImage) && learningEnabled) {
                if (isAudio && sttEnabled) {
                    try {
                        let audioBuffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                        const mimeType = msg.message.audioMessage?.mimetype || 'audio/ogg'
                        const transcription = await transcribeAudio(audioBuffer, mimeType, configs)
                        audioBuffer = null
                        if (transcription) { bufferText = `[AUDIO] ${transcription}`; mediaContent = { type: 'audio', transcription } }
                    } catch (e) { console.error('[STT] Erro:', e.message) }
                }
                if (isImage && configs.vision?.enabled !== false) {
                    try {
                        let imgBuffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                        const caption = msg.message.imageMessage?.caption || ''
                        const description = await analyzeImage(imgBuffer, caption, configs)
                        imgBuffer = null
                        if (description) { bufferText = `[IMAGEM] ${description}${caption ? ` | "${caption}"` : ''}`; mediaContent = { type: 'image', description, caption } }
                    } catch (e) { console.error('[VISION] Erro:', e.message) }
                }
            }

            if (bufferText.length > 2) {
                buffer.messages.push({ author, authorJid, text: bufferText })
                console.log(`[BUFFER] ${author}: "${bufferText.substring(0, 60)}" (${buffer.messages.length}/${MAX_BUFFER_MESSAGES}) ativo:${isActiveGroup}`)
            }

            // ── Dispara aprendizado quando buffer cheio ──
            if (buffer.messages.length >= MAX_BUFFER_MESSAGES && learningEnabled) {
                const msgs = [...buffer.messages]
                buffer.messages = []
                learnFromConversation(tenantId, remoteJid, msgs, configs, isActiveGroup).catch(e =>
                    console.error('[LEARN BG] Erro:', e.message)
                )
            }

            if (!isBotEnabled) continue

            // ── Decisão de responder ──
            // PV: sempre
            // Grupo: mencionado, reply, keyword, nome da IA no texto
            const shouldRespond = isPV ||
                (isGroup && (isMentioned || isReplyToMe || containsKeyword || mentionedByName))

            let contentForAI = textContent
            if (mediaContent?.type === 'audio') contentForAI = `[Mensagem de voz] ${mediaContent.transcription}`
            else if (mediaContent?.type === 'image') contentForAI = `[Imagem] ${mediaContent.description}${textContent ? ` | "${textContent}"` : ''}`

            if (!contentForAI.trim() && !mediaContent) continue
            if (!shouldRespond) continue

            console.log(`🤖 [${isGroup ? 'GRUPO' : 'PV'}] ${tenantId} → ${remoteJid} (ativo:${isActiveGroup})`)

            // ── Reação automática (antes de responder) ──
            const reactionEmoji = pickReaction(null, textContent)
            if (reactionEmoji && Math.random() < 0.45) {
                sendReaction(sock, msg, reactionEmoji).catch(() => {})
                // Pequeno delay natural após reagir
                await humanDelay(300 + Math.random() * 700)
            }

            // ── Busca contexto, perfis e memória longa ──
            let convMemory = '', senderProfile = '', longTermMemory = '', currentVibe = 'Neutro'

            try {
                const { data: convData } = await supabase.from('whatsapp_conversation_contexts')
                    .select('summary, vibe, group_type, daily_topics, communication_style, context_hint')
                    .eq('tenant_id', tenantId).eq('conversation_id', remoteJid).single()

                if (convData?.summary) {
                    convMemory  = `\n[CONTEXTO DA CONVERSA: ${convData.summary}]`
                    if (convData.group_type)         convMemory += `\n[TIPO DE GRUPO: ${convData.group_type}]`
                    if (convData.daily_topics)       convMemory += `\n[TÓPICOS DO MOMENTO: ${convData.daily_topics}]`
                    if (convData.communication_style) convMemory += `\n[ESTILO/GÍRIAS DO GRUPO: ${convData.communication_style}]`
                    if (convData.context_hint)       convMemory += `\n[DICA IMPORTANTE: ${convData.context_hint}]`
                }
                if (convData?.vibe) currentVibe = convData.vibe

                const { data: profData } = await supabase.from('whatsapp_contact_profiles')
                    .select('full_name, nickname, personality_traits, communication_style, metadata')
                    .eq('tenant_id', tenantId).eq('contact_id', authorJid).single()

                if (profData) {
                    const nicks = profData.metadata?.nicknames || []
                    senderProfile = `\n[PERFIL DE ${author}: nome=${profData.full_name || author}${nicks.length ? `, apelidos: ${nicks.join(', ')}` : ''}, personalidade: ${profData.personality_traits || 'N/A'}, estilo: ${profData.communication_style || 'N/A'}]`
                }

                // Busca memória de longo prazo
                longTermMemory = await getContactMemory(tenantId, authorJid, 8)

            } catch (_) {}

            // ── Personalidade específica por grupo ──
            let groupPersonality = null
            if (isGroup) {
                groupPersonality = await getGroupPersonality(tenantId, remoteJid)
                if (groupPersonality?.enabled === false) {
                    console.log(`[SKIP] Grupo ${remoteJid} com IA desativada por config`)
                    continue
                }
            }

            // ── Intenção simples (resposta rápida sem IA) ──
            const simpleIntent = detectSimpleIntent(contentForAI)

            // ── Limpa menções do texto ──
            let cleanText = contentForAI
                .replace(new RegExp(`@${botId}`, 'g'), '')
                .replace(new RegExp(`@${botLidShort}`, 'g'), '')
                .trim()
            if (containsKeyword && keyword) {
                cleanText = cleanText.replace(new RegExp(keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), '').trim()
            }

            // ── Monta prompt com tudo ──
            const chatCfg       = configs.chat || {}
            const basePrompt    = groupPersonality?.system_prompt || chatCfg.system_prompt || configs.system_prompt || 'Você é um assistente.'
            const ttsActive     = configs.tts?.enabled === true || configs.tts?.enabled === 'true'
            const ttsInstruction = ttsActive ? '\n[TTS ATIVO: escreva como se fosse FALAR em voz alta. Use pontuação natural — vírgulas, pontos, reticências. Evite listas e markdown.]' : ''
            const activeGroupCtx = isActiveGroup ? '\n[GRUPO MOVIMENTADO: conversa ativa, seja mais espontânea e presente]' : ''
            const groupCtx = isGroup
                ? `\n[VOCÊ ESTÁ NUM GRUPO. Seja natural, como membro da galera. Vibe atual: ${currentVibe}. NUNCA diga que é IA.]`
                : `\n[CONVERSA PRIVADA. Seja atencioso, carinhoso e direto.]`
            const styleInstruction = `\n[ESTILO: vibe = ${currentVibe}. Use gírias do grupo se descontraído. Seja HUMANO. Grupo → resposta curta e natural. PV → pode ser mais detalhado.]`
            const sizeInstruction = `\n[TAMANHO: varie o tamanho de forma humana — às vezes 2-3 palavras, às vezes 1-2 frases. Nunca responda da mesma forma sempre.]`

            let businessCtx = ''
            if (tenantId !== ADMIN_TENANT_ID) businessCtx = await getTenantContext(tenantId)

            const resolvedMemory    = await resolveNames(convMemory, tenantId, sock)
            const resolvedCleanText = await resolveNames(cleanText, tenantId, sock)

            const fullPrompt = `${businessCtx ? `EMPRESA:\n${businessCtx}\n\n` : ''}INSTRUÇÕES:\n${basePrompt}${resolvedMemory}${longTermMemory}${senderProfile}${groupCtx}${activeGroupCtx}${styleInstruction}${sizeInstruction}${ttsInstruction}\n\nMENSAGEM DE ${author}: ${resolvedCleanText || 'Oi!'}`

            try {
                const response = await getAIResponse(fullPrompt, configs)
                if (response) {
                    // Às vezes envia sticker junto (só em grupos animados)
                    if (isGroup && isActiveGroup && Math.random() < 0.08) {
                        const mood = currentVibe.toLowerCase().includes('zoeira') ? 'laugh'
                            : currentVibe.toLowerCase().includes('animad') ? 'happy' : null
                        if (mood) {
                            await sendSticker(sock, remoteJid, mood)
                            await humanDelay(600 + Math.random() * 800)
                        }
                    }
                    await sendSmartResponse(sock, remoteJid, response, msg, configs)
                } else {
                    if (isPV) await sock.sendMessage(remoteJid, { text: 'Sem conexão com o modelo agora, tenta de novo!' }, { quoted: msg })
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

app.get('/status',   (_, res) => res.redirect('/status/admin'))
app.post('/start',   (_, res) => res.redirect(307, '/start/admin'))
app.post('/stop',    (_, res) => res.redirect(307, '/stop/admin'))
app.get('/groups',   (_, res) => res.redirect('/groups/admin'))
app.post('/send',    (_, res) => res.redirect(307, '/send/admin'))
app.post('/reset',   (_, res) => res.redirect(307, '/reset/admin'))
app.post('/ai/reload', async (_, res) => { await loadTenantAIConfigs(ADMIN_TENANT_ID); res.json({ success: true }) })

app.get('/status/:tenantId', (req, res) => {
    const s = sessions.get(req.params.tenantId)
    if (!s) return res.json({ status: 'disconnected', qr: null })
    res.json({ status: s.status, qr: s.lastQr })
})

app.post('/start/:tenantId', async (req, res) => {
    try {
        const { tenantId } = req.params
        const existing = sessions.get(tenantId)
        if (existing && existing.status !== 'disconnected' && existing.status !== 'error')
            return res.json({ success: true, message: 'Sessão já ativa.', status: existing.status })

        const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)
        let forceReset = false
        if (fs.existsSync(authPath) && !fs.existsSync(path.join(authPath, 'creds.json'))) {
            fs.rmSync(authPath, { recursive: true, force: true })
            forceReset = true
        }
        res.json({ success: true, message: 'Iniciando...', status: 'connecting' })
        connectToWhatsApp(tenantId, forceReset).catch(err => console.error(`[BG] Falha:`, err))
    } catch (err) {
        if (!res.headersSent) res.status(500).json({ success: false, error: err.message })
    }
})

app.post('/stop/:tenantId', async (req, res) => {
    const { tenantId } = req.params
    const session = sessions.get(tenantId)
    const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)
    if (session?.sock) {
        try { await session.sock.logout(); res.json({ success: true }) }
        catch (e) { sessions.delete(tenantId); if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true }); res.json({ success: true }) }
    } else {
        if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true })
        res.json({ success: true })
    }
})

app.post('/reset/:tenantId', async (req, res) => {
    const { tenantId } = req.params
    const session = sessions.get(tenantId)
    if (session?.sock) { try { session.sock.end() } catch (_) {} }
    sessions.delete(tenantId)
    const authPath = path.join(__dirname, `auth_info_baileys/tenant_${tenantId}`)
    if (fs.existsSync(authPath)) fs.rmSync(authPath, { recursive: true, force: true })
    try { await supabase.from('whatsapp_sessions').delete().eq('tenant_id', tenantId) } catch (_) {}
    res.json({ success: true, message: 'Sessão resetada.' })
})

app.post('/ai/reload/:tenantId', async (req, res) => { await loadTenantAIConfigs(req.params.tenantId); res.json({ success: true }) })

app.post('/ai/list-models', async (req, res) => {
    const { api_key, provider } = req.body
    if (!api_key || !provider) return res.status(400).json({ error: 'api_key e provider obrigatórios' })
    try {
        let apiUrl = '', headers = {}
        if (provider === 'gemini')           apiUrl = `https://generativelanguage.googleapis.com/v1beta/models?key=${api_key}`
        else if (provider === 'groq')        { apiUrl = 'https://api.groq.com/openai/v1/models'; headers = { Authorization: `Bearer ${api_key}` } }
        else if (provider === 'openrouter')  { apiUrl = 'https://openrouter.ai/api/v1/models'; headers = { Authorization: `Bearer ${api_key}` } }
        else if (provider === 'nvidia')      { apiUrl = 'https://integrate.api.nvidia.com/v1/models'; headers = { Authorization: `Bearer ${api_key}` } }
        else if (provider === 'openai')      { apiUrl = 'https://api.openai.com/v1/models'; headers = { Authorization: `Bearer ${api_key}` } }
        else if (provider === 'kimi' || provider === 'moonshot') { apiUrl = 'https://api.moonshot.ai/v1/models'; headers = { Authorization: `Bearer ${api_key}` } }
        else if (provider === 'deepseek')    { apiUrl = 'https://api.deepseek.com/v1/models'; headers = { Authorization: `Bearer ${api_key}` } }
        else if (provider === 'ollama') {
            const ollamaUrl = process.env.OLLAMA_PROXY_URL || 'http://localhost:11434'
            try { const r = await fetch(`${ollamaUrl}/api/tags`); const d = await r.json(); return res.json({ success: true, models: (d.models || []).map(m => ({ id: m.name, name: m.name })) }) }
            catch (e) { return res.status(500).json({ error: `Ollama offline: ${e.message}` }) }
        } else return res.status(400).json({ error: 'Provider inválido' })

        const response = await fetch(apiUrl, { headers })
        const data = await response.json()
        if (data.error) throw new Error(data.error.message || 'Erro ao buscar modelos')
        let models = []
        if (provider === 'gemini') models = (data.models || []).filter(m => m.supportedGenerationMethods?.includes('generateContent')).map(m => ({ id: m.name.replace('models/', ''), name: m.displayName || m.name.replace('models/', '') }))
        else models = (data.data || []).map(m => ({ id: m.id, name: m.id }))
        res.json({ success: true, models })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

app.get('/groups/:tenantId', async (req, res) => {
    const session = sessions.get(req.params.tenantId)
    if (!session || session.status !== 'authenticated') return res.status(503).json({ success: false, error: 'Não conectado' })
    try {
        const groupMetadata = await session.sock.groupFetchAllParticipating()
        res.json({ success: true, groups: Object.values(groupMetadata).map(g => ({ id: g.id, subject: g.subject })) })
    } catch (err) { res.status(500).json({ success: false, error: err.message }) }
})

app.post('/send/:tenantId', async (req, res) => {
    const session = sessions.get(req.params.tenantId)
    if (!session || session.status !== 'authenticated') return res.status(503).json({ success: false, error: 'Não conectado' })
    try {
        const { number, message } = req.body
        if (!number || !message) return res.status(400).json({ error: 'number e message obrigatórios' })
        let jid = number
        if (!jid.includes('@')) jid = (jid.includes('-') || jid.length > 15) ? `${jid}@g.us` : `${jid}@s.whatsapp.net`
        await session.sock.sendMessage(jid, { text: message })
        res.json({ success: true })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

// ── Endpoint para agendamentos ──
app.get('/schedules', async (_, res) => {
    try {
        const { data } = await supabase.from('whatsapp_schedules').select('*').order('send_time')
        res.json({ success: true, schedules: data || [] })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

app.post('/schedules', async (req, res) => {
    try {
        const { data, error } = await supabase.from('whatsapp_schedules').insert(req.body).select().single()
        if (error) throw error
        res.json({ success: true, schedule: data })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

app.delete('/schedules/:id', async (req, res) => {
    try {
        await supabase.from('whatsapp_schedules').delete().eq('id', req.params.id)
        res.json({ success: true })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

// ── Endpoint para configs por grupo ──
app.get('/group-configs/:tenantId', async (req, res) => {
    try {
        const { data } = await supabase.from('whatsapp_group_configs').select('*').eq('tenant_id', req.params.tenantId)
        res.json({ success: true, configs: data || [] })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

app.post('/group-configs', async (req, res) => {
    try {
        const { data, error } = await supabase.from('whatsapp_group_configs').upsert(req.body, { onConflict: 'tenant_id, group_jid' }).select().single()
        if (error) throw error
        res.json({ success: true, config: data })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

// ── Endpoint para fila de handoff ──
app.get('/handoff', async (_, res) => {
    try {
        const { data } = await supabase.from('whatsapp_handoff_queue').select('*').eq('status', 'pending').order('created_at', { ascending: false })
        res.json({ success: true, queue: data || [] })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

app.post('/handoff/:id/resolve', async (req, res) => {
    try {
        await supabase.from('whatsapp_handoff_queue').update({ status: 'resolved', resolved_at: new Date() }).eq('id', req.params.id)
        res.json({ success: true })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

// ── Endpoint para memória longa ──
app.get('/memory/:tenantId/:contactJid', async (req, res) => {
    try {
        const { data } = await supabase.from('whatsapp_long_term_memory')
            .select('*').eq('tenant_id', req.params.tenantId).eq('contact_jid', req.params.contactJid)
            .order('created_at', { ascending: false }).limit(30)
        res.json({ success: true, memories: data || [] })
    } catch (err) { res.status(500).json({ error: err.message }) }
})

// ══════════════════════════════════════════════════
// AUTO-START + SCHEDULE LOOP
// ══════════════════════════════════════════════════
async function autoStartSavedSessions() {
    await new Promise(r => setTimeout(r, 3000))
    try {
        const { data: savedSessions, error } = await supabase
            .from('whatsapp_sessions').select('tenant_id').eq('status', 'authenticated')
        if (error) { console.error('[AUTO-START] Erro:', error.message); return }
        if (!savedSessions?.length) { console.log('[AUTO-START] Nenhuma sessão salva.'); return }
        console.log(`[AUTO-START] 🔄 ${savedSessions.length} sessão(ões) para restaurar...`)
        for (const { tenant_id } of savedSessions) {
            const credsFile = path.join(__dirname, `auth_info_baileys/tenant_${tenant_id}/creds.json`)
            if (fs.existsSync(credsFile)) {
                console.log(`[AUTO-START] ✅ Restaurando: ${tenant_id}`)
                connectToWhatsApp(tenant_id, false).catch(err => console.error(`[AUTO-START] ❌ ${tenant_id}:`, err.message))
                await new Promise(r => setTimeout(r, 2000))
            } else {
                console.log(`[AUTO-START] ⚠️  ${tenant_id}: sem creds.json`)
            }
        }
    } catch (err) { console.error('[AUTO-START] Exceção:', err.message) }

    // Inicia loop de agendamentos (verifica a cada minuto)
    if (scheduleInterval) clearInterval(scheduleInterval)
    scheduleInterval = setInterval(runScheduledMessages, 60000)
    console.log('[SCHEDULE] ⏰ Loop de agendamentos iniciado')
}

const PORT = process.env.WHATSAPP_PORT || 3001
app.listen(PORT, () => {
    console.log(`🚀 RED IA WhatsApp Service v3.0 — porta ${PORT}`)
    autoStartSavedSessions()
})
