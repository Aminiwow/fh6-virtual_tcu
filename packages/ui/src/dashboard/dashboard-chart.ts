import type { ShiftGuideCurvePoint, TelemetrySnapshot } from '@virtual-tcu/shared/types/telemetry'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const PAD = { top: 20, right: 48, bottom: 34, left: 50 }

export interface ChartLegendItem {
  key: string
  label: string
  color: string
  value: string
}

function niceMax(value: number) {
  if (value <= 0)
    return 1
  const magnitude = 10 ** Math.floor(Math.log10(value))
  return Math.ceil(value / magnitude) * magnitude
}

function normalizePoint(point: ShiftGuideCurvePoint, minRpm: number, rpmSpan: number) {
  return (point.rpm - minRpm) / Math.max(1, rpmSpan)
}

export function useDashboardChart(getTelemetry: () => TelemetrySnapshot | null) {
  const canvasRef = ref<HTMLCanvasElement | null>(null)
  const latest = computed(() => getTelemetry())

  const curve = computed(() => latest.value?.shift_guide?.curve ?? [])
  const legend = computed<ChartLegendItem[]>(() => {
    const guide = latest.value?.shift_guide
    return [
      {
        key: 'hp',
        label: 'HP',
        color: '#22d3ee',
        value: guide?.peak_hp ? `${Math.round(guide.peak_hp)}` : '--',
      },
      {
        key: 'torque',
        label: 'TQ',
        color: '#f59e0b',
        value: guide?.peak_torque_nm ? `${Math.round(guide.peak_torque_nm)} Nm` : '--',
      },
      {
        key: 'shift',
        label: 'SHIFT',
        color: '#4ade80',
        value: guide?.gears?.some((g) => g.upshift_rpm) ? 'targets' : '--',
      },
    ]
  })

  function drawSeries(
    ctx: CanvasRenderingContext2D,
    points: ShiftGuideCurvePoint[],
    valueKey: 'hp' | 'torque_nm',
    color: string,
    minRpm: number,
    rpmSpan: number,
    valueMax: number,
    plotW: number,
    plotH: number,
  ) {
    if (points.length < 2)
      return
    ctx.strokeStyle = color
    ctx.lineWidth = 2
    ctx.lineJoin = 'round'
    ctx.beginPath()
    points.forEach((point, idx) => {
      const x = PAD.left + normalizePoint(point, minRpm, rpmSpan) * plotW
      const y = PAD.top + plotH - (point[valueKey] / valueMax) * plotH
      if (idx === 0)
        ctx.moveTo(x, y)
      else
        ctx.lineTo(x, y)
    })
    ctx.stroke()
  }

  function draw() {
    const canvas = canvasRef.value
    if (!canvas || canvas.clientWidth === 0 || canvas.clientHeight === 0)
      return

    const ctx = canvas.getContext('2d')
    if (!ctx)
      return

    if (canvas.width !== canvas.clientWidth || canvas.height !== canvas.clientHeight) {
      canvas.width = canvas.clientWidth
      canvas.height = canvas.clientHeight
    }

    const W = canvas.width
    const H = canvas.height
    const plotW = Math.max(1, W - PAD.left - PAD.right)
    const plotH = Math.max(1, H - PAD.top - PAD.bottom)
    const points = curve.value
    const guide = latest.value?.shift_guide
    const engineMax = guide?.engine_max_rpm ?? latest.value?.rpm_max ?? 8000
    const minRpm = Math.max(0, guide?.rpm_min ?? points[0]?.rpm ?? 0)
    const maxRpm = Math.max(minRpm + 1000, guide?.rpm_max_seen ?? engineMax)
    const rpmSpan = maxRpm - minRpm
    const powerMax = niceMax(Math.max(...points.map((p) => p.hp), guide?.peak_hp ?? 0, 1))
    const torqueMax = niceMax(Math.max(...points.map((p) => p.torque_nm), guide?.peak_torque_nm ?? 0, 1))

    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = '#090b10'
    ctx.fillRect(0, 0, W, H)

    ctx.font = '10px ui-monospace, monospace'
    ctx.textBaseline = 'middle'

    for (let i = 0; i <= 4; i++) {
      const y = PAD.top + (plotH / 4) * i
      ctx.strokeStyle = 'rgba(255,255,255,0.07)'
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(PAD.left, y)
      ctx.lineTo(W - PAD.right, y)
      ctx.stroke()

      ctx.fillStyle = 'rgba(255,255,255,0.42)'
      ctx.textAlign = 'right'
      ctx.fillText(`${Math.round(powerMax * (1 - i / 4))}`, PAD.left - 8, y)
      ctx.textAlign = 'left'
      ctx.fillText(`${Math.round(torqueMax * (1 - i / 4))}`, W - PAD.right + 8, y)
    }

    for (let i = 0; i <= 4; i++) {
      const pct = i / 4
      const x = PAD.left + plotW * pct
      const rpm = minRpm + rpmSpan * pct
      ctx.fillStyle = 'rgba(113,113,122,0.95)'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      ctx.fillText(`${Math.round(rpm)}`, x, H - PAD.bottom + 10)
    }

    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
    ctx.fillStyle = '#22d3ee'
    ctx.fillText('HP', 10, 8)
    ctx.textAlign = 'right'
    ctx.fillStyle = '#f59e0b'
    ctx.fillText('Nm', W - 10, 8)

    if (points.length < 2) {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle = 'rgba(255,255,255,0.45)'
      ctx.font = '12px ui-monospace, monospace'
      ctx.fillText('No learned power curve yet', W / 2, H / 2)
      return
    }

    drawSeries(ctx, points, 'hp', '#22d3ee', minRpm, rpmSpan, powerMax, plotW, plotH)
    drawSeries(ctx, points, 'torque_nm', '#f59e0b', minRpm, rpmSpan, torqueMax, plotW, plotH)

    for (const gear of guide?.gears ?? []) {
      if (!gear.upshift_rpm)
        continue
      const x = PAD.left + ((gear.upshift_rpm - minRpm) / Math.max(1, rpmSpan)) * plotW
      if (x < PAD.left || x > W - PAD.right)
        continue
      ctx.strokeStyle = 'rgba(74,222,128,0.55)'
      ctx.lineWidth = 1
      ctx.setLineDash([4, 4])
      ctx.beginPath()
      ctx.moveTo(x, PAD.top)
      ctx.lineTo(x, PAD.top + plotH)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = '#4ade80'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'bottom'
      ctx.fillText(`${gear.gear}->${gear.next_gear ?? gear.gear + 1}`, x, PAD.top + plotH - 4)
    }

    const markers = [
      { rpm: guide?.peak_hp_rpm, color: '#22d3ee' },
      { rpm: guide?.peak_torque_rpm, color: '#f59e0b' },
    ]
    for (const marker of markers) {
      if (!marker.rpm)
        continue
      const x = PAD.left + ((marker.rpm - minRpm) / Math.max(1, rpmSpan)) * plotW
      if (x < PAD.left || x > W - PAD.right)
        continue
      ctx.strokeStyle = marker.color
      ctx.lineWidth = 1.5
      ctx.beginPath()
      ctx.moveTo(x, PAD.top)
      ctx.lineTo(x, PAD.top + plotH)
      ctx.stroke()
    }
  }

  let ro: ResizeObserver | null = null

  onMounted(() => {
    ro = new ResizeObserver(() => draw())
    watch(
      canvasRef,
      (el) => {
        if (el)
          ro?.observe(el)
      },
      { immediate: true },
    )
  })

  onUnmounted(() => ro?.disconnect())

  watch(
    () => latest.value?.shift_guide,
    () => draw(),
    { deep: true },
  )

  return { canvasRef, legend, draw }
}
