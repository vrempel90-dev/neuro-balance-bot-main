import OpenAI from 'openai'

export function getOpenAI(): OpenAI {
  if (!process.env.OPENAI_API_KEY) {
    throw new Error('Missing OPENAI_API_KEY environment variable')
  }
  return new OpenAI({ apiKey: process.env.OPENAI_API_KEY })
}

/**
 * Транскрипция голосового сообщения через Whisper.
 * @param buffer - содержимое .ogg / .mp3 / .wav файла
 * @param filename - имя файла с расширением (whisper определяет формат по нему)
 * @param lang - 'ru' | 'kk' (для казахского)
 */
export async function transcribeVoice(
  buffer: Buffer,
  filename: string = 'voice.ogg',
  lang: string = 'ru',
): Promise<string> {
  const openai = getOpenAI()
  const file = new File([new Uint8Array(buffer)], filename, {
    type: filename.endsWith('.mp3') ? 'audio/mpeg' : 'audio/ogg',
  })
  const transcription = await openai.audio.transcriptions.create({
    model: 'whisper-1',
    file,
    language: lang,
  })
  return transcription.text.trim()
}
