import { NextResponse } from 'next/server'
import { getAvailableSlots } from '@/lib/crm-queries'

// GET /api/bot/check-slots?date=YYYY-MM-DD&doctor=login
// Возвращает свободные слоты для записи

export async function GET(req: Request) {
  try {
    const secret = req.headers.get('x-bot-secret')
    if (secret !== process.env.BOT_API_SECRET) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const { searchParams } = new URL(req.url)
    const date = searchParams.get('date')
    const doctor = searchParams.get('doctor') || undefined

    if (!date) {
      return NextResponse.json({ error: 'date parameter required (YYYY-MM-DD)' }, { status: 400 })
    }

    const availability = await getAvailableSlots(date, doctor)

    // Возвращаем только доступные слоты для упрощения ответа боту
    const result = availability.map(a => ({
      doctorLogin: a.doctorLogin,
      doctorName: a.doctorName,
      date: a.date,
      availableSlots: a.slots.filter(s => s.available).map(s => s.time),
    }))

    return NextResponse.json({ availability: result })
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : 'Internal error'
    console.error('GET /api/bot/check-slots:', error)
    return NextResponse.json({ error: message }, { status: 500 })
  }
}
