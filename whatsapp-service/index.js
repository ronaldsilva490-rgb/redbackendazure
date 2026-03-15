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
const EventEmitter = require('events')
const eventEmitter = new EventEmitter()

const WebSocket = require('ws')
let proxySocket = null
const proxyUrl = process.env.RED_PROXY_URL || 'ws://automais.ddns.net:11434'

function initProxyConnection() {
    if (proxySocket && (proxySocket.readyState === WebSocket.OPEN || proxySocket.readyState === WebSocket.CONNECTING)) return
    
    console.log(`[PROXY] Conectando ao RED Proxy: ${proxyUrl}`)
    proxySocket = new WebSocket(proxyUrl)

    proxySocket.onopen = () => {
        console.log(`[PROXY] Conectado ao RED Proxy com sucesso.`)
        proxySocket.send(JSON.stringify({
            action: 'STATUS',
            agent: 'LOCAL_FRONTEND',
            sessionId: 'WHATSAPP_SERVICE'
        }))
    }

    proxySocket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data)
            eventEmitter.emit('proxy_message', data)
        } catch (e) {}
    }

    proxySocket.onclose = () => {
        console.log(`[PROXY] Conexão fechada. Tentando reconectar em 5s...`)
        setTimeout(initProxyConnection, 5000)
    }

    proxySocket.onerror = (err) => {
        console.error(`[PROXY] Erro na conexão:`, err.message)
    }
}

initProxyConnection()

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
const waSessionDispatchState = new Map()

// ── Controle de proatividade e atividade por conversa ──
const lastProactiveTime     = new Map() // tenantId_jid → timestamp último proativo
const lastRealtimeAnalysis  = new Map() // tenantId_jid → timestamp última análise realtime
const groupActivityWindow   = new Map() // tenantId_jid → [timestamps]

// ── Defaults (substituídos pelos valores do dashboard) ──
const DEFAULT_BUFFER_SIZE         = 6      // msgs pra acumular antes do learn completo
const DEFAULT_PROACTIVE_COOLDOWN  = 40000  // ms entre intervenções proativas
const DEFAULT_REALTIME_COOLDOWN   = 8000   // ms mínimo entre análises realtime por conversa
const DEFAULT_ACTIVITY_WINDOW_MS  = 120000 // janela pra medir grupo ativo
const DEFAULT_ACTIVE_THRESH       = 4      // msgs na janela = grupo ativo

/** Lê config dinâmica com fallback pro default — null-safe */
function getCfg(configs, key, defaultVal) {
    try {
        if (!configs) return defaultVal
        const val = configs?.proactive?.[key] ?? configs?.[key]
        if (val === undefined || val === null || val === '') return defaultVal
        const n = parseFloat(val)
        return isNaN(n) ? defaultVal : n
    } catch (_) { return defaultVal }
}

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
function trackGroupActivity(key, configs = {}) {
    const now = Date.now()
    if (!groupActivityWindow.has(key)) groupActivityWindow.set(key, [])
    const times  = groupActivityWindow.get(key)
    const window = getCfg(configs, 'activity_window_ms', DEFAULT_ACTIVITY_WINDOW_MS)
    const thresh = getCfg(configs, 'active_group_thresh', DEFAULT_ACTIVE_THRESH)
    times.push(now)
    const cutoff = now - window
    while (times.length && times[0] < cutoff) times.shift()
    return times.length >= thresh
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
const realtimeStreamState = new Map()
const STREAM_PRESENCE_REFRESH_MS = 4000
const STREAM_PRESENCE_IDLE_MS = 18000
const STREAM_STATE_RETENTION_MS = 120000

function streamStateKey(tenantId, remoteJid) {
    return `${tenantId || 'default'}::${remoteJid || 'unknown'}`
}

function streamDataToText(streamData) {
    if (!streamData?.chunks?.length) return ''
    return streamData.chunks.map((c) => {
        if (!c || typeof c !== 'object') return ''
        if (c.type === 'text' && typeof c.content === 'string') {
            return c.content.trim()
        }
        if (c.type === 'heading' && typeof c.content === 'string') {
            return `*${c.content.trim()}*`
        }
        if (c.type === 'code' && typeof c.content === 'string') {
            const lang = typeof c.language === 'string' && c.language.trim() ? c.language.trim() : ''
            return lang ? `\`\`\`${lang}\n${c.content.trim()}\n\`\`\`` : `\`\`\`\n${c.content.trim()}\n\`\`\``
        }
        if (c.type === 'list' && Array.isArray(c.items)) {
            return c.items
                .map((item, idx) => c.listType === 'ol' ? `${idx + 1}. ${String(item).trim()}` : `• ${String(item).trim()}`)
                .join('\n')
                .trim()
        }
        return ''
    }).filter(Boolean).join('\n\n').trim()
}

function touchRealtimeStreamBuffer(key, streamEvent) {
    if (!key) return
    const now = Date.now()
    const current = realtimeStreamState.get(key) || {
        text: '',
        updates: 0,
        startedAt: now,
        lastEventAt: now,
        composingLastTouch: 0,
        composingInterval: null
    }
    const chunkText = streamDataToText(streamEvent?.data)
    if (chunkText) current.text = chunkText
    current.updates += 1
    current.lastEventAt = now
    current.composingLastTouch = now
    realtimeStreamState.set(key, current)
}

function startRealtimeComposing(sock, remoteJid, key) {
    if (!sock || !remoteJid || !key) return
    const state = realtimeStreamState.get(key) || {
        text: '',
        updates: 0,
        startedAt: Date.now(),
        lastEventAt: Date.now(),
        composingLastTouch: Date.now(),
        composingInterval: null
    }
    state.composingLastTouch = Date.now()
    const sendComposing = async () => {
        try { await sock.sendPresenceUpdate('composing', remoteJid) } catch (_) {}
    }
    if (!state.composingInterval) {
        sendComposing()
        state.composingInterval = setInterval(() => {
            const current = realtimeStreamState.get(key)
            if (!current) return
            if (Date.now() - (current.composingLastTouch || 0) > STREAM_PRESENCE_IDLE_MS) {
                clearInterval(current.composingInterval)
                current.composingInterval = null
                return
            }
            sendComposing()
        }, STREAM_PRESENCE_REFRESH_MS)
    }
    realtimeStreamState.set(key, state)
}

async function stopRealtimeComposing(sock, remoteJid, key) {
    const state = realtimeStreamState.get(key)
    if (!state) return
    if (state.composingInterval) {
        clearInterval(state.composingInterval)
        state.composingInterval = null
    }
    try { await sock.sendPresenceUpdate('available', remoteJid) } catch (_) {}
    const snapshot = { ...state }
    setTimeout(() => {
        const latest = realtimeStreamState.get(key)
        if (!latest) return
        if (latest.startedAt !== snapshot.startedAt) return
        realtimeStreamState.delete(key)
    }, STREAM_STATE_RETENTION_MS)
}

function handleRealtimeAIStreamEvent(sock, remoteJid, key, eventData) {
    if (!eventData || !key) return
    touchRealtimeStreamBuffer(key, eventData)
    if (eventData.action === 'NEURAL_STREAM' || eventData.action === 'NEURAL_COMPLETE') {
        startRealtimeComposing(sock, remoteJid, key)
    }
}

function escapeRegExp(str) {
    return String(str).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function softenSourceReferences(text) {
    const knownLabels = [
        'Pravda em português',
        'Folha de S.Paulo',
        'BBC News Brasil',
        'A Referência',
        'CNN Brasil',
        'Descomplica',
        'Estadão',
        'Reuters',
        'Datafolha',
        'UOL',
        'G1'
    ]
    const sortedLabels = [...knownLabels].sort((a, b) => b.length - a.length)
    return String(text)
        .split('\n')
        .map((rawLine) => {
            let line = rawLine
            for (const label of sortedLabels) {
                const regex = new RegExp(`(\\s+)(${escapeRegExp(label)})\\s*$`, 'i')
                const match = line.match(regex)
                if (!match) continue
                line = line.replace(regex, ` (_${match[2]}_)`)
                break
            }
            return line
        })
        .join('\n')
}

function formatForWhatsApp(text) {
    if (!text) return ''
    let t = String(text)
    t = t.replace(/<br\s*\/?>/gi, '\n')
    t = t.replace(/<\/p>\s*<p>/gi, '\n\n')
    t = t.replace(/<p[^>]*>/gi, '')
    t = t.replace(/<\/p>/gi, '')
    t = t.replace(/<li[^>]*>\s*/gi, '• ')
    t = t.replace(/<\/li>/gi, '\n')
    t = t.replace(/<strong[^>]*>([\s\S]*?)<\/strong>/gi, '*$1*')
    t = t.replace(/<b[^>]*>([\s\S]*?)<\/b>/gi, '*$1*')
    t = t.replace(/<em[^>]*>([\s\S]*?)<\/em>/gi, '_$1_')
    t = t.replace(/<i[^>]*>([\s\S]*?)<\/i>/gi, '_$1_')
    t = t.replace(/<code[^>]*>([\s\S]*?)<\/code>/gi, '```$1```')
    t = t.replace(/<[^>]+>/g, '')
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '$1 ($2)')
    t = t.replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    t = softenSourceReferences(t)
    t = t.replace(/\n{3,}/g, '\n\n')
    return t.trim()
}

async function sendSmartResponse(sock, remoteJid, text, quotedMsg, configs, extraOpts = {}) {
    const attachmentFiles = Array.isArray(extraOpts.files) ? extraOpts.files : []
    const waText = formatForWhatsApp(text)
    const ttsCfg = configs.tts || {}
    const ttsEnabled = ttsCfg.enabled === true || ttsCfg.enabled === 'true'
    const shouldSendAudio = ttsEnabled && Math.random() < (parseFloat(ttsCfg.audio_probability) || 0.3)

    if (shouldSendAudio && waText.length < 500 && attachmentFiles.length === 0) {
        try {
            await sock.sendPresenceUpdate('recording', remoteJid)
            const audioBuffer = await generateAudio(waText, configs)
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
        await humanDelay(Math.min(4000, Math.max(800, waText.length * 30)))
        await sock.sendPresenceUpdate('available', remoteJid)
    } catch (_) {}

    if (attachmentFiles.length) {
        const sentWithCaption = await sendRemoteFilesToWhatsApp(sock, remoteJid, attachmentFiles, quotedMsg, waText)
        if (!sentWithCaption && waText) {
            await sock.sendMessage(remoteJid, { text: waText }, { quoted: quotedMsg })
            await sendRemoteFilesToWhatsApp(sock, remoteJid, attachmentFiles, quotedMsg, '')
        }
        return
    }

    if (waText) await sock.sendMessage(remoteJid, { text: waText }, { quoted: quotedMsg })
}

async function sendRemoteFilesToWhatsApp(sock, remoteJid, files, quotedMsg, captionText = '') {
    let sentAny = false
    let captionPending = !!captionText
    for (const f of files) {
        try {
            let buffer = null
            if (typeof f?.dataBase64 === 'string' && f.dataBase64.trim()) {
                buffer = Buffer.from(f.dataBase64, 'base64')
            } else {
                const url = typeof f?.url === 'string' ? f.url.trim() : ''
                if (!/^https?:\/\//i.test(url)) continue
                const resp = await fetch(url, { method: 'GET' })
                if (!resp.ok) continue
                const arr = await resp.arrayBuffer()
                buffer = Buffer.from(arr)
                if (!f?.mimeType) f.mimeType = resp.headers.get('content-type') || 'application/octet-stream'
                if (!f?.name) {
                    try { f.name = decodeURIComponent(new URL(url).pathname.split('/').filter(Boolean).pop() || 'arquivo') } catch (_) { f.name = 'arquivo' }
                }
            }
            if (!buffer.length) continue
            let fileName = typeof f?.name === 'string' ? f.name.trim() : ''
            if (!fileName) {
                fileName = 'arquivo'
            }
            const mime = f?.mimeType || 'application/octet-stream'
            const message = { document: buffer, fileName, mimetype: mime }
            if (captionPending) {
                message.caption = captionText
                captionPending = false
            }
            await sock.sendMessage(remoteJid, message, { quoted: quotedMsg })
            sentAny = true
        } catch (_) {}
    }
    return sentAny
}

function normalizeIncomingFiles(files) {
    if (!Array.isArray(files)) return []
    return files
        .filter(f => (typeof f?.url === 'string' && f.url.trim()) || (typeof f?.dataBase64 === 'string' && f.dataBase64.trim()))
        .map(f => ({
            name: typeof f?.name === 'string' && f.name.trim() ? f.name.trim() : 'arquivo',
            url: typeof f?.url === 'string' ? f.url.trim() : '',
            mimeType: typeof f?.mimeType === 'string' && f.mimeType.trim() ? f.mimeType.trim() : 'application/octet-stream',
            dataBase64: typeof f?.dataBase64 === 'string' ? f.dataBase64.trim() : ''
        }))
}

function fileFingerprint(file) {
    const name = (file?.name || 'arquivo').trim().toLowerCase()
    const mime = (file?.mimeType || 'application/octet-stream').trim().toLowerCase()
    const url = (file?.url || '').trim()
    const b64Len = typeof file?.dataBase64 === 'string' ? file.dataBase64.length : 0
    return `${name}|${mime}|${url}|${b64Len}`
}

async function handleAsyncFilesFromProxy(data) {
    if (data?.action !== 'NEURAL_FILES_READY') return
    const sessionId = typeof data?.sessionId === 'string' ? data.sessionId : ''
    if (!sessionId || !sessionId.startsWith('WA_')) return
    const state = waSessionDispatchState.get(sessionId)
    if (!state?.sock || !state?.remoteJid) return
    const incoming = normalizeIncomingFiles(data.files)
    if (!incoming.length) return
    const newFiles = incoming.filter((f) => {
        const fp = fileFingerprint(f)
        if (state.sentFileFingerprints.has(fp)) return false
        state.sentFileFingerprints.add(fp)
        return true
    })
    if (!newFiles.length) return
    try {
        await sendRemoteFilesToWhatsApp(state.sock, state.remoteJid, newFiles, null, '')
        state.updatedAt = Date.now()
    } catch (_) {}
}

eventEmitter.on('proxy_message', (data) => {
    handleAsyncFilesFromProxy(data).catch(() => {})
})

// ══════════════════════════════════════════════════
// CORE IA — Geração de Resposta
// ══════════════════════════════════════════════════
async function getAIResponse(prompt, configs, overrideSystemPrompt = null, options = {}) {
    const chatCfg = configs.chat || {}
    
    // FORÇAR RED-CLAUDE SE HOUVER INSTANCE ID
    const instanceId = chatCfg.red_instance_id || configs.red_instance_id;
    const isRedClaudeForced = !!instanceId;
    
    const provider = isRedClaudeForced ? 'red-claude' : (chatCfg.provider || configs.ai_provider || 'gemini');
    
    const apiKey = chatCfg.api_key || configs.api_key || ''
    const model = chatCfg.model || configs.model || ''
    const systemPrompt = overrideSystemPrompt ?? chatCfg.system_prompt ?? configs.system_prompt ?? ''

    if (provider === 'red-claude') {
        if (!instanceId) {
            console.error(`[AI] RED Claude selecionado para ${configs.tenant_id || 'unknown'}, mas red_instance_id não encontrado.`);
            return null;
        }

        const sessionId = `WA_${configs.tenant_id || 'default'}`;
        
        return new Promise((resolve) => {
            if (!proxySocket || proxySocket.readyState !== WebSocket.OPEN) {
                console.error("[AI] Proxy RED não está conectado.");
                return resolve(null);
            }

            let finished = false
            const responseHandler = (data) => {
                if ((data.action === 'NEURAL_STREAM' || data.action === 'NEURAL_COMPLETE') && data.sessionId === sessionId) {
                    if (typeof options?.onStream === 'function') {
                        try { options.onStream(data) } catch (_) {}
                    }
                }
                if (data.action === 'NEURAL_COMPLETE' && data.sessionId === sessionId) {
                    finished = true
                    eventEmitter.off('proxy_message', responseHandler);
                    let text = typeof data.text === 'string' ? data.text : ''
                    let files = []
                    if (Array.isArray(data.files)) {
                        files = data.files
                            .filter(f => (typeof f?.url === 'string' && f.url) || (typeof f?.dataBase64 === 'string' && f.dataBase64))
                            .map(f => ({
                                name: typeof f.name === 'string' ? f.name : 'arquivo',
                                url: typeof f.url === 'string' ? f.url : '',
                                mimeType: typeof f.mimeType === 'string' ? f.mimeType : 'application/octet-stream',
                                dataBase64: typeof f.dataBase64 === 'string' ? f.dataBase64 : ''
                            }))
                    }
                    if (!text && data?.data?.chunks?.length) {
                        text = streamDataToText(data.data)
                    }
                    if (!files.length && data?.data?.chunks?.length) {
                        const seen = new Set()
                        files = data.data.chunks
                            .filter(c => c?.type === 'file' && typeof c.url === 'string')
                            .map(c => ({ name: typeof c.name === 'string' ? c.name : 'arquivo', url: c.url, mimeType: 'application/octet-stream', dataBase64: '' }))
                            .filter(f => /^https?:\/\//i.test(f.url) && !seen.has(f.url) && seen.add(f.url))
                    }
                    if (options?.includeMeta) resolve({ text: text || null, files })
                    else resolve(text || null);
                }
            };

            eventEmitter.on('proxy_message', responseHandler);

            const proxyText = systemPrompt ? `${systemPrompt}\n\n${prompt}` : prompt

            proxySocket.send(JSON.stringify({
                action: "START_NEURAL_LINK",
                text: proxyText,
                instanceId: instanceId,
                sessionId: sessionId
            }));

            // Timeout ampliado para respostas com geração de arquivos
            setTimeout(() => {
                if (finished) return
                eventEmitter.off('proxy_message', responseHandler);
                resolve(null);
            }, 180000);
        });
    }

    // Se não for RED Claude, validar chaves para outros providers
    if (!apiKey && provider !== 'ollama') {
        console.warn(`[AI] Config incompleta (${provider})`);
                return options?.includeMeta ? { text: null, files: [] } : null;
    }

    try {
        if (provider === 'gemini') {
            const genAI = new GoogleGenerativeAI(apiKey)
            const mdl = genAI.getGenerativeModel({ model, systemInstruction: systemPrompt })
            const result = await mdl.generateContent(prompt)
            const out = result.response.text()
            return options?.includeMeta ? { text: out, files: [] } : out
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
        if (data.error) { console.error(`[AI] Erro ${provider}:`, data.error); return options?.includeMeta ? { text: null, files: [] } : null }
        const out = data.choices?.[0]?.message?.content || null
        return options?.includeMeta ? { text: out, files: [] } : out
    } catch (err) {
        console.error(`[AI] Exceção (${provider}):`, err.message)
        return options?.includeMeta ? { text: null, files: [] } : null
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
    const apiKey  = chatCfg.api_key || aiConfigs.api_key || ''
    if (!apiKey) return

    console.log(`[LEARN] 🧠 ${conversationId} (ativo: ${isActiveGroup})`)

    try {
        const { data: currentContext } = await supabase
            .from('whatsapp_conversation_contexts')
            .select('*')
            .eq('tenant_id', tenantId)
            .eq('conversation_id', conversationId)
            .single()

        const oldSummary   = currentContext?.summary || 'Sem histórico.'
        const transcript   = newMessages.map(m => `${m.author}: ${m.text}`).join('\n')
        const personality  = chatCfg.system_prompt || 'Você é um membro humano do grupo.'
        const activeHint   = isActiveGroup
            ? 'GRUPO ATIVO: participe se o assunto for interessante, seja ousada.'
            : 'Só participe se houver algo genuinamente relevante.'

        const proactiveCfgForAI = aiConfigs.proactive || {}
        const baseChatCfg = (proactiveCfgForAI.provider && proactiveCfgForAI.api_key && proactiveCfgForAI.model)
            ? { provider: proactiveCfgForAI.provider, api_key: proactiveCfgForAI.api_key, model: proactiveCfgForAI.model, system_prompt: personality }
            : { ...chatCfg }
        const analysisCfg = { ...aiConfigs, chat: baseChatCfg }

        // ── CHAMADA 1: Contexto + perfis (JSON menor, mais estável) ──
        const contextPrompt = `Analise esta conversa de WhatsApp e retorne APENAS JSON puro.

RESUMO ANTERIOR: "${oldSummary}"
MENSAGENS:
${transcript}

JSON com exatamente estas chaves:
{"summary":"resumo curto","vibe":"humor atual","group_type":"tipo","daily_topics":"topicos","style":"girias","profiles":[{"jid":"...","name":"...","nicknames":[],"memory_facts":[]}],"intent":"conversa","handoff_needed":false,"context_hint":"dica curta"}`

        const ctxResponse = await getAIResponse(contextPrompt, analysisCfg, 'Responda APENAS com JSON puro. Sem texto antes ou depois.')
        let ctxResult = null
        if (ctxResponse) {
            try {
                const j = ctxResponse.replace(/```json\s*/gi, '').replace(/```/g, '').trim()
                const s = j.substring(j.indexOf('{'), j.lastIndexOf('}') + 1)
                ctxResult = JSON.parse(s)
            } catch (e) {
                console.warn('[LEARN] Parse contexto falhou:', e.message)
            }
        }

        // ── CHAMADA 2: Decisão proativa (JSON mínimo, 3 campos apenas) ──
        const proactivePrompt = `Você é a IA com esta personalidade: "${personality}"
${activeHint}

CONVERSA:
${transcript}

Decida se deve enviar uma mensagem espontânea AGORA.
Retorne APENAS este JSON (sem texto extra, sem markdown):
{"thought":"mensagem curta natural se quiser participar, vazio se não","urgency":0,"trigger":""}

Regras para "thought":
- Máximo 2 frases curtas, linguagem natural do grupo
- Use as gírias que aparecem na conversa
- NUNCA comece com "Olá" ou saudações formais
- Deixe VAZIO se não houver nada genuíno a dizer
Regras para "urgency": número 0-10 (0=silêncio, 7+=vale participar, 10=imperdível)
Regras para "trigger": o que motivou (ex: "pergunta aberta", "piada", "polêmica") ou vazio`

        const proactiveResponse = await getAIResponse(proactivePrompt, analysisCfg, 'Responda APENAS com JSON puro de 3 campos.')
        let proResult = { thought: '', urgency: 0, trigger: '' }
        if (proactiveResponse) {
            try {
                const j = proactiveResponse.replace(/```json\s*/gi, '').replace(/```/g, '').trim()
                const s = j.substring(j.indexOf('{'), j.lastIndexOf('}') + 1)
                const parsed = JSON.parse(s)
                proResult = {
                    thought:  (parsed.thought  || parsed.proactive_thought  || '').trim(),
                    urgency:  parseFloat(parsed.urgency  || parsed.proactive_urgency  || 0),
                    trigger:  parsed.trigger   || parsed.proactive_trigger  || '',
                }
            } catch (e) {
                console.warn('[LEARN] Parse proativo falhou:', e.message)
                // Recuperação por regex mesmo
                const mThought = proactiveResponse.match(/"thought"\s*:\s*"((?:[^"\\]|\\.)*)"/)?.[1] || ''
                const mUrgency = proactiveResponse.match(/"urgency"\s*:\s*([0-9.]+)/)?.[1] || '0'
                proResult = { thought: mThought.trim(), urgency: parseFloat(mUrgency), trigger: '' }
            }
        }

        console.log(`[LEARN] ✅ vibe:${ctxResult?.vibe || '?'} | urgency:${proResult.urgency} | thought:"${proResult.thought?.substring(0,50)}"`)

        // ── Salva contexto ──
        if (ctxResult) {
            await supabase.from('whatsapp_conversation_contexts').upsert({
                tenant_id: tenantId,
                conversation_id: conversationId,
                summary: ctxResult.summary || oldSummary,
                vibe: ctxResult.vibe || 'Neutro',
                group_type: ctxResult.group_type || 'Geral',
                daily_topics: ctxResult.daily_topics || '',
                communication_style: ctxResult.style || '',
                context_hint: ctxResult.context_hint || '',
                updated_at: new Date()
            }, { onConflict: 'tenant_id, conversation_id' })

            // Atualiza perfis + memória longa
            if (ctxResult.profiles?.length) {
                for (const p of ctxResult.profiles) {
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
                        metadata: meta,
                        updated_at: new Date()
                    }, { onConflict: 'tenant_id, contact_id' })

                    if (p.memory_facts?.length) {
                        for (const fact of p.memory_facts) {
                            if (fact?.length > 5) await saveMemoryFact(tenantId, p.jid, fact)
                        }
                    }
                }
            }

            // Handoff
            if (ctxResult.handoff_needed) {
                try {
                    await supabase.from('whatsapp_handoff_queue').upsert({
                        tenant_id: tenantId, conversation_id: conversationId,
                        reason: ctxResult.intent || 'Detectado pela IA', status: 'pending', created_at: new Date()
                    }, { onConflict: 'tenant_id, conversation_id' })
                } catch (_) {}
            }
        }

        // ── Disparo proativo ──
        const proactiveThought = proResult.thought
        const urgency          = proResult.urgency
        const trigger          = proResult.trigger

        const lastProactiveKey     = `${tenantId}_${conversationId}`
        const timeSinceLastProactive = Date.now() - (lastProactiveTime.get(lastProactiveKey) || 0)

        const proactiveCfg      = aiConfigs.proactive || {}
        const proactiveEnabled  = proactiveCfg.enabled !== false && proactiveCfg.enabled !== 'false'
        const frequency         = parseFloat(proactiveCfg.frequency || 0.15)
        const effectiveFreq     = isActiveGroup ? Math.min(frequency * 3, 0.85) : frequency

        // Cooldown adaptativo por urgência
        const cooldownMs      = getCfg(aiConfigs, 'proactive_cooldown_ms', DEFAULT_PROACTIVE_COOLDOWN)
        const adaptiveCooldown = urgency >= 9 ? 8000 : urgency >= 7 ? 15000 : urgency >= 5 ? 25000 : cooldownMs

        const roll = Math.random()
        const shouldParticipate = proactiveEnabled
            && proactiveThought.length > 3
            && timeSinceLastProactive > adaptiveCooldown
            && (
                urgency >= 9
                || (urgency >= 7 && roll < 0.90)
                || (urgency >= 5 && roll < effectiveFreq * 2)
                || (urgency >= 3 && roll < effectiveFreq)
                || (isActiveGroup  && roll < effectiveFreq * 0.5)
            )

        console.log(`[PROATIVO] urgency:${urgency} roll:${roll.toFixed(2)} freq:${effectiveFreq.toFixed(2)} ativo:${isActiveGroup} enabled:${proactiveEnabled} thought:"${proactiveThought?.substring(0,40)}" cooldownOK:${timeSinceLastProactive > adaptiveCooldown} → ${shouldParticipate ? '✅ VAI' : '❌ não'}`)

        if (shouldParticipate) {
            const session = sessions.get(tenantId)
            if (session?.sock && session.status === 'authenticated') {
                lastProactiveTime.set(lastProactiveKey, Date.now())

                const minDelay = urgency >= 8 ? 1000 : urgency >= 5 ? 2000 : 3500
                const maxDelay = urgency >= 8 ? 3000 : urgency >= 5 ? 6000 : 12000
                const delay    = minDelay + Math.random() * (maxDelay - minDelay)

                setTimeout(async () => {
                    try {
                        // proactiveThought é instrução interna do LEARN — não vai direto pro WhatsApp
                        // Passa pela IA de chat para gerar a mensagem final humanizada
                        const chatPersonalityPrompt = (aiConfigs.chat || {}).system_prompt || 'Você é um membro humano do grupo.'
                        const chatPrompt = `${chatPersonalityPrompt}
[VOCÊ VAI PARTICIPAR ESPONTANEAMENTE DA CONVERSA]
[INTENÇÃO: ${proactiveThought}]
[ESTILO: natural, use gírias do grupo se houver, máx 2 frases, NUNCA comece com saudação formal, NUNCA mencione que é IA]

Escreva SUA FALA para entrar na conversa agora:`

                        const finalMsg = await getAIResponse(chatPrompt, aiConfigs)
                        if (finalMsg && finalMsg.trim().length > 2) {
                            await sendSmartResponse(session.sock, conversationId, finalMsg.trim(), null, aiConfigs)
                            console.log(`[PROATIVO] ✅ Enviado: "${finalMsg.trim().substring(0,60)}"`)
                        }
                    } catch (e) {
                        console.error('[PROATIVO] Erro ao enviar:', e.message)
                    }
                }, delay)
            }
        }

    } catch (err) {
        console.error(`[LEARN] ❌ Erro:`, err.message)
    }
}

// ══════════════════════════════════════════════════
// ANÁLISE REALTIME — decisão proativa por mensagem
// ══════════════════════════════════════════════════
async function realtimeProactiveAnalysis(tenantId, conversationId, messageText, author, configs, sock, isActiveGroup) {
    const proactiveCfg = configs.proactive || {}
    if (proactiveCfg.enabled === false || proactiveCfg.enabled === 'false') return
    if (!messageText || messageText.length < 3) return

    const chatCfg = configs.chat || {}
    const apiKey  = chatCfg.api_key || configs.api_key || ''
    if (!apiKey) return

    const rtKey      = `rt_${tenantId}_${conversationId}`
    const rtCooldown = getCfg(configs, 'realtime_cooldown_ms', DEFAULT_REALTIME_COOLDOWN)
    if (Date.now() - (lastRealtimeAnalysis.get(rtKey) || 0) < rtCooldown) return
    lastRealtimeAnalysis.set(rtKey, Date.now())

    const proKey     = `${tenantId}_${conversationId}`
    const cooldownMs = getCfg(configs, 'proactive_cooldown_ms', DEFAULT_PROACTIVE_COOLDOWN)
    if (Date.now() - (lastProactiveTime.get(proKey) || 0) < cooldownMs) return

    const personality = chatCfg.system_prompt || 'Voce e um membro humano do grupo.'
    const activeHint  = isActiveGroup
        ? 'O grupo esta MOVIMENTADO. Seja mais propensa a participar.'
        : 'So participe se for algo realmente relevante.'
    const frequency   = parseFloat(proactiveCfg.frequency || 0.15)

    // ── PASSO 1: Análise interna — decide SE e SOBRE O QUÊ participar ──
    const analysisPrompt = `${activeHint}

${author} disse: "${messageText}"

Analise esta mensagem e retorne SOMENTE este JSON (sem markdown):
{"should_reply":false,"urgency":0,"topic":"","angle":"","trigger":""}

- should_reply: true se vale participar, false se nao
- urgency: 0-10
- topic: tema da mensagem em poucas palavras (ex: "risada sem contexto", "pergunta sobre Matrix")
- angle: como a IA deve abordar (ex: "entrar na brincadeira", "dar opiniao direta", "fazer uma pergunta de volta")
- trigger: "pergunta","piada","polemica","nome_mencionado","celebracao" ou vazio`

    try {
        const analysisResponse = await getAIResponse(analysisPrompt, configs, 'Analista interno. Responda APENAS com JSON de 5 campos.')
        if (!analysisResponse) return

        const j = analysisResponse.replace(/```json\s*/gi, '').replace(/```/g, '').trim()
        const s = j.substring(j.indexOf('{'), j.lastIndexOf('}') + 1)
        let analysis
        try {
            analysis = JSON.parse(s)
        } catch (_) {
            const mR = analysisResponse.match(/"should_reply"\s*:\s*(true|false)/)?.[1]
            const mU = analysisResponse.match(/"urgency"\s*:\s*([0-9.]+)/)?.[1] || '0'
            const mT = analysisResponse.match(/"topic"\s*:\s*"((?:[^"\\]|\\.)*)"/)?.[1] || ''
            const mA = analysisResponse.match(/"angle"\s*:\s*"((?:[^"\\]|\\.)*)"/)?.[1] || ''
            analysis = { should_reply: mR === 'true', urgency: parseFloat(mU), topic: mT, angle: mA, trigger: '' }
        }

        const urgency  = parseFloat(analysis.urgency || 0)
        const topic    = (analysis.topic  || '').trim()
        const angle    = (analysis.angle  || '').trim()
        const trigger  = analysis.trigger || ''

        if (!analysis.should_reply || urgency < 1) return

        const urgencyThresh = isActiveGroup
            ? getCfg(configs, 'realtime_urgency_active', 5)
            : getCfg(configs, 'realtime_urgency_idle',   7)

        const roll        = Math.random()
        const effectiveFreq = isActiveGroup ? Math.min(frequency * 2.5, 0.80) : frequency

        const shouldFire = urgency >= urgencyThresh && (
            urgency >= 9
            || (urgency >= 7 && roll < 0.88)
            || (urgency >= 5 && roll < effectiveFreq * 1.5)
            || roll < effectiveFreq
        )

        console.log(`[RT] ${author} | urgency:${urgency} roll:${roll.toFixed(2)} thresh:${urgencyThresh} ativo:${isActiveGroup} topic:"${topic}" angle:"${angle}" -> ${shouldFire ? 'VAI' : 'nao'}`)

        if (!shouldFire || !sock) return

        // ── PASSO 2: Gera a mensagem FINAL com a IA de chat ──
        // O "thought" do passo 1 é instrução interna — nunca vai direto pro WhatsApp
        lastProactiveTime.set(proKey, Date.now())

        const minDelay = urgency >= 8 ? 800  : urgency >= 5 ? 1500 : 3000
        const maxDelay = urgency >= 8 ? 2500 : urgency >= 5 ? 5000 : 10000
        const delay    = minDelay + Math.random() * (maxDelay - minDelay)

        setTimeout(async () => {
            try {
                // Busca contexto da conversa para enriquecer a resposta
                let convContext = ''
                try {
                    const { data: ctx } = await supabase
                        .from('whatsapp_conversation_contexts')
                        .select('vibe, style, context_hint')
                        .eq('tenant_id', tenantId)
                        .eq('conversation_id', conversationId)
                        .single()
                    if (ctx?.vibe)        convContext += `\n[VIBE DO GRUPO: ${ctx.vibe}]`
                    if (ctx?.style)       convContext += `\n[GÍRIAS DO GRUPO: ${ctx.style}]`
                    if (ctx?.context_hint) convContext += `\n[CONTEXTO: ${ctx.context_hint}]`
                } catch (_) {}

                const chatPrompt = `${personality}${convContext}
[VOCÊ VAI PARTICIPAR ESPONTANEAMENTE DA CONVERSA]
[TEMA: ${topic}]
[ABORDAGEM: ${angle}]
[${author} disse: "${messageText}"]
[ESTILO: seja natural, use as gírias do grupo se houver, máx 2 frases, NUNCA comece com saudação formal, NUNCA mencione que é IA]

Escreva SUA FALA para entrar na conversa agora:`

                const finalMsg = await getAIResponse(chatPrompt, configs)
                if (finalMsg && finalMsg.trim().length > 2) {
                    await sendSmartResponse(sock, conversationId, finalMsg.trim(), null, configs)
                    console.log(`[RT] ✅ Enviado: "${finalMsg.trim().substring(0,60)}"`)
                }
            } catch (e) {
                console.error('[RT] Erro ao gerar/enviar:', e.message)
            }
        }, delay)
    } catch (err) {
        console.error('[RT] Excecao:', err.message)
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
        const isAdmin = tenantId === ADMIN_TENANT_ID || tenantId === 'admin'

        // Busca na tabela de integração (onde o dashboard sempre salva)
        const { data: tenantDataArr } = await supabase.from('whatsapp_tenant_configs').select('*').eq('tenant_id', tenantId).limit(1)
        let d = tenantDataArr?.[0] || {}

        // SE FOR ADMIN E ESTIVER VAZIO: Busca em qualquer registro que tenha o ID da instância (Fallback mestre)
        if (isAdmin && !d.red_instance_id) {
            const { data: fallbackData } = await supabase
                .from('whatsapp_tenant_configs')
                .select('*')
                .not('red_instance_id', 'eq', '')
                .not('red_instance_id', 'is', null)
                .limit(1)
            if (fallbackData?.[0]) {
                d = {
                    ...d,
                    red_instance_id: fallbackData[0].red_instance_id || d.red_instance_id || '',
                    red_proxy_url: fallbackData[0].red_proxy_url || d.red_proxy_url || ''
                }
                console.log(`[PROXY] 🔄 Fallback: Usando red_instance_id do tenant ${fallbackData[0].tenant_id}`)
            }
        }

        if (isAdmin) {
            const { data: globalData, error } = await supabase.from('ai_configs').select('*')
            const configs = {}
            if (!error && globalData) {
                globalData.forEach(item => {
                    if (item.key) configs[item.key] = item.value
                })
            }

            // PRIORIDADE: Se existe config no painel de WhatsApp, usa o prompt de lá (mesmo que vazio).
            const hasTenantConfig = !!tenantDataArr?.[0]
            const provider = d.ai_provider || configs.ai_provider || 'gemini'

            configData = {
                tenant_id: tenantId,
                chat: {
                    provider: d.chat_provider || d.ai_provider || configs.chat_provider || provider,
                    api_key: d.chat_api_key || d.api_key || configs[`${configs.chat_provider || provider}_api_key`] || configs[`${provider}_api_key`] || process.env.GEMINI_API_KEY || '',
                    model: d.chat_model || d.model || configs.chat_model || configs[`${provider}_model`] || 'gemini-2.0-flash',
                    system_prompt: hasTenantConfig ? (d.system_prompt || '') : (d.system_prompt || configs.chat_system_prompt || configs[`${provider}_system_prompt`] || ''),
                    red_instance_id: d.red_instance_id || configs.red_instance_id || '',
                    red_proxy_url: d.red_proxy_url || configs.red_proxy_url || 'ws://automais.ddns.net:11434'
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
                    model: configs.proactive_model || configs.chat_model || '',
                    // Parâmetros dinâmicos configuráveis pelo dashboard
                    buffer_size:            parseInt(configs.buffer_size)            || DEFAULT_BUFFER_SIZE,
                    proactive_cooldown_ms:  parseInt(configs.proactive_cooldown_ms)  || DEFAULT_PROACTIVE_COOLDOWN,
                    realtime_cooldown_ms:   parseInt(configs.realtime_cooldown_ms)   || DEFAULT_REALTIME_COOLDOWN,
                    activity_window_ms:     parseInt(configs.activity_window_ms)     || DEFAULT_ACTIVITY_WINDOW_MS,
                    active_group_thresh:    parseInt(configs.active_group_thresh)    || DEFAULT_ACTIVE_THRESH,
                    realtime_urgency_active: parseInt(configs.realtime_urgency_active) || 5,
                    realtime_urgency_idle:   parseInt(configs.realtime_urgency_idle)   || 7,
                    realtime_enabled:       configs.realtime_enabled !== 'false',
                },
                ai_provider: provider,
                api_key: configs[`${provider}_api_key`] || process.env.GEMINI_API_KEY || '',
                model: configs[`${provider}_model`] || 'gemini-2.0-flash',
                system_prompt: configs[`${provider}_system_prompt`] || '',
                ai_prefix: configs.ai_prefix || '',
                ai_bot_enabled: configs.ai_bot_enabled === 'true',
                red_instance_id: configs.red_instance_id || '',
                red_proxy_url: configs.red_proxy_url || ''
            }
        } else {
            configData = {
                tenant_id: tenantId,
                chat: {
                    provider: d.chat_provider || d.ai_provider || 'gemini',
                    api_key: d.chat_api_key || d.api_key || '',
                    model: d.chat_model || d.model || '',
                    system_prompt: d.system_prompt || '',
                    red_instance_id: d.red_instance_id || '',
                    red_proxy_url: d.red_proxy_url || 'ws://automais.ddns.net:11434'
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
                    model: d.proactive_model || d.model || '',
                    buffer_size:            parseInt(d.buffer_size)            || DEFAULT_BUFFER_SIZE,
                    proactive_cooldown_ms:  parseInt(d.proactive_cooldown_ms)  || DEFAULT_PROACTIVE_COOLDOWN,
                    realtime_cooldown_ms:   parseInt(d.realtime_cooldown_ms)   || DEFAULT_REALTIME_COOLDOWN,
                    activity_window_ms:     parseInt(d.activity_window_ms)     || DEFAULT_ACTIVITY_WINDOW_MS,
                    active_group_thresh:    parseInt(d.active_group_thresh)    || DEFAULT_ACTIVE_THRESH,
                    realtime_urgency_active: parseInt(d.realtime_urgency_active) || 5,
                    realtime_urgency_idle:   parseInt(d.realtime_urgency_idle)   || 7,
                    realtime_enabled:       d.realtime_enabled !== false,
                },
                ai_provider: d.ai_provider || 'gemini',
                api_key: d.api_key || '',
                model: d.model || '',
                system_prompt: d.system_prompt || '',
                ai_prefix: d.ai_prefix || '',
                ai_bot_enabled: d.ai_enabled === true,
                red_instance_id: d.red_instance_id || '',
                red_proxy_url: d.red_proxy_url || ''
            }
        }

        const session = sessions.get(tenantId)
        if (session) {
            session.aiConfigs = configData
            const instanceLog = configData.chat?.red_instance_id ? ` (ID: ${configData.chat.red_instance_id})` : ''
            console.log(`✅ Configs [${tenantId}] Chat: ${configData.chat?.provider}${instanceLog}/${configData.chat?.model}`)
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

    const session = { sock, aiConfigs: null, lastQr: null, status: 'connecting', lastConfigRefresh: 0 }
    sessions.set(tenantId, session)
    await loadTenantAIConfigs(tenantId)
    session.lastConfigRefresh = Date.now()

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

            if ((Date.now() - (session.lastConfigRefresh || 0)) > 10000) {
                await loadTenantAIConfigs(tenantId)
                session.lastConfigRefresh = Date.now()
            }
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
            const isActiveGroup = isGroup && trackGroupActivity(bufferKey, configs)

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

            const bufferSize = getCfg(configs, 'buffer_size', DEFAULT_BUFFER_SIZE)

            if (bufferText.length > 2) {
                buffer.messages.push({ author, authorJid, text: bufferText })
                console.log(`[BUFFER] ${author}: "${bufferText.substring(0, 60)}" (${buffer.messages.length}/${bufferSize}) ativo:${isActiveGroup}`)
            }

            // ── Análise realtime por mensagem (em paralelo, não bloqueia) ──
            const realtimeEnabled = configs.proactive?.realtime_enabled !== false
            if (isBotEnabled && isGroup && realtimeEnabled && bufferText.length > 3) {
                const session2 = sessions.get(tenantId)
                realtimeProactiveAnalysis(
                    tenantId, remoteJid, bufferText, author, configs,
                    session2?.sock && session2.status === 'authenticated' ? session2.sock : null,
                    isActiveGroup
                ).catch(e => console.error('[RT BG] Erro:', e.message))
            }

            // ── Dispara aprendizado completo quando buffer cheio ──
            if (buffer.messages.length >= bufferSize && learningEnabled) {
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
                const liveStateKey = streamStateKey(tenantId, remoteJid)
                const aiSessionId = `WA_${tenantId || 'default'}`
                waSessionDispatchState.set(aiSessionId, {
                    sock,
                    remoteJid,
                    updatedAt: Date.now(),
                    sentFileFingerprints: new Set()
                })
                const aiResult = await getAIResponse(fullPrompt, configs, null, {
                    includeMeta: true,
                    onStream: (eventData) => handleRealtimeAIStreamEvent(sock, remoteJid, liveStateKey, eventData)
                })
                const response = typeof aiResult === 'string' ? aiResult : (aiResult?.text || null)
                const files = Array.isArray(aiResult?.files) ? aiResult.files : []
                const state = waSessionDispatchState.get(aiSessionId)
                if (state) {
                    files.forEach((f) => state.sentFileFingerprints.add(fileFingerprint(f)))
                    state.updatedAt = Date.now()
                }
                if (response || files.length) {
                    // Às vezes envia sticker junto (só em grupos animados)
                    if (isGroup && isActiveGroup && Math.random() < 0.08) {
                        const mood = currentVibe.toLowerCase().includes('zoeira') ? 'laugh'
                            : currentVibe.toLowerCase().includes('animad') ? 'happy' : null
                        if (mood) {
                            await sendSticker(sock, remoteJid, mood)
                            await humanDelay(600 + Math.random() * 800)
                        }
                    }
                    await sendSmartResponse(sock, remoteJid, response || '📎 Arquivo gerado.', msg, configs, { files })
                } else {
                    if (isPV) await sock.sendMessage(remoteJid, { text: 'Sem conexão com o modelo agora, tenta de novo!' }, { quoted: msg })
                }
                setTimeout(() => {
                    const latest = waSessionDispatchState.get(aiSessionId)
                    if (!latest) return
                    if (Date.now() - (latest.updatedAt || 0) >= 180000) {
                        waSessionDispatchState.delete(aiSessionId)
                    }
                }, 185000)
                await stopRealtimeComposing(sock, remoteJid, liveStateKey)
            } catch (err) {
                await stopRealtimeComposing(sock, remoteJid, streamStateKey(tenantId, remoteJid))
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
