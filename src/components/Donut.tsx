import type { Context } from 'konva/lib/Context'
import { useMemo } from 'react'
import { Group, Shape } from 'react-konva'
import type { IconProps } from '../@types/iconProps'

const Donut: React.FC<IconProps> = ({ data }) => {
  const scale = (data.size ?? 100) / 100
  const opacity = (100 - (data.transparency ?? 0)) / 100
  const outerRadius = 512
  const innerRadius = (data.donutRadius ?? 0) * 2
  const arcAngle = data.arcAngle ?? 360

  //[TODO] 形状计算优化,现在差一点点

  const sceneFunc = useMemo(() => {
    return (ctx: Context, shape: any) => {
      const angleRad = (arcAngle * Math.PI) / 180
      const startAngle = -Math.PI / 2 // 从上方中心点开始
      const endAngle = startAngle + angleRad

      ctx.beginPath()

      if (arcAngle === 360) {
        // 完整圆环
        ctx.arc(0, 0, outerRadius, 0, Math.PI * 2, false)
        ctx.arc(0, 0, innerRadius, 0, Math.PI * 2, true) // 逆时针绘制内圆形成空心
      } else {
        // 扇形圆环
        // 外圆弧
        ctx.arc(0, 0, outerRadius, startAngle, endAngle, false)
        // 内圆弧（逆向），会自动连接到内圆起始点
        ctx.arc(0, 0, innerRadius, endAngle, startAngle, true)
        // 闭合路径
        ctx.closePath()
      }

      ctx.fillStrokeShape(shape)
    }
  }, [arcAngle, outerRadius, innerRadius])

  const { offsetX, offsetY } = useMemo(() => {
    if (arcAngle === 360) {
      return { offsetX: 0, offsetY: 0 }
    }

    const angleRad = (arcAngle * Math.PI) / 180
    const startAngle = -Math.PI / 2
    const endAngle = startAngle + angleRad

    // 计算扇形的边界框
    let minX = Infinity
    let maxX = -Infinity
    let minY = Infinity
    let maxY = -Infinity

    const points = []

    // 外圆弧端点
    points.push({
      x: outerRadius * Math.cos(startAngle),
      y: outerRadius * Math.sin(startAngle),
    })
    points.push({
      x: outerRadius * Math.cos(endAngle),
      y: outerRadius * Math.sin(endAngle),
    })

    // 内圆弧端点
    points.push({
      x: innerRadius * Math.cos(startAngle),
      y: innerRadius * Math.sin(startAngle),
    })
    points.push({
      x: innerRadius * Math.cos(endAngle),
      y: innerRadius * Math.sin(endAngle),
    })

    // 检查圆弧是否穿过关键角度点（0°, 90°, 180°, 270°）
    const checkAngle = (angle: number) => {
      const normalized = ((angle % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)
      const start = ((startAngle % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)
      let end = ((endAngle % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)

      if (end < start) end += 2 * Math.PI
      const check = normalized < start ? normalized + 2 * Math.PI : normalized

      return check >= start && check <= end
    }

    // 0° (右)
    if (checkAngle(0)) points.push({ x: outerRadius, y: 0 })
    // 90° (下)
    if (checkAngle(Math.PI / 2)) points.push({ x: 0, y: outerRadius })
    // 180° (左)
    if (checkAngle(Math.PI)) points.push({ x: -outerRadius, y: 0 })
    // 270° (上)
    if (checkAngle((3 * Math.PI) / 2)) points.push({ x: 0, y: -outerRadius })

    points.forEach((p) => {
      minX = Math.min(minX, p.x)
      maxX = Math.max(maxX, p.x)
      minY = Math.min(minY, p.y)
      maxY = Math.max(maxY, p.y)
    })

    const centerX = (minX + maxX) / 2
    const centerY = (minY + maxY) / 2

    return { offsetX: centerX, offsetY: centerY }
  }, [arcAngle, outerRadius, innerRadius])

  return (
    <Group
      x={data.x * 2}
      y={data.y * 2 - 10}
      scaleX={scale}
      scaleY={scale}
      opacity={opacity}
      offsetX={offsetX}
      offsetY={offsetY}
    >
      <Shape
        sceneFunc={sceneFunc}
        fill="orange"
        stroke="darkorange"
        strokeWidth={2}
      />
    </Group>
  )
}

export default Donut
