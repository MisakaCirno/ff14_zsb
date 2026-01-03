import type { SceneContext } from 'konva/lib/Context'
import { useMemo } from 'react'
import { Group, Image } from 'react-konva'
import useImage from 'use-image'
import type { IconProps } from '../@types/iconProps'
import { getIconUrl } from '../utils/staticImage'

const circleSrc = getIconUrl('circle_aoe')
const CircleAoe: React.FC<IconProps> = ({ data }) => {
  const [img] = useImage(circleSrc)

  const scale = (data.size ?? 100) / 100
  const opacity = data.hidden ? 0 : (100 - (data.transparency ?? 0)) / 100

  const arcAngle = data.type === 'fan_aoe' ? data.arcAngle ?? 90 : 360

  const clipFunc = useMemo(() => {
    if (arcAngle === 360) return undefined
    return (ctx: SceneContext) => {
      const r = 512
      const angleRad = (arcAngle * Math.PI) / 180
      const startAngle = -Math.PI / 2
      const endAngle = -Math.PI / 2 + angleRad

      ctx.beginPath()
      ctx.moveTo(512, 512)
      ctx.arc(512, 512, r, startAngle, endAngle)
      ctx.closePath()
    }
  }, [arcAngle])

  // 根据裁剪后的图片计算offset
  const { offsetX, offsetY } = useMemo(() => {
    if (arcAngle === 360) {
      // 圆形，中心点在图片中心
      return { offsetX: 512, offsetY: 512 }
    }

    // 扇形，计算实际边界
    const r = 512
    const angleRad = (arcAngle * Math.PI) / 180
    const startAngle = -Math.PI / 2
    const endAngle = -Math.PI / 2 + angleRad

    // 计算扇形的边界框
    let minX = 512
    let maxX = 512
    let minY = 512
    let maxY = 512

    // 圆弧上的起点和终点
    const startX = 512 + r * Math.cos(startAngle)
    const startY = 512 + r * Math.sin(startAngle)
    const endX = 512 + r * Math.cos(endAngle)
    const endY = 512 + r * Math.sin(endAngle)

    minX = Math.min(minX, startX, endX)
    maxX = Math.max(maxX, startX, endX)
    minY = Math.min(minY, startY, endY)
    maxY = Math.max(maxY, startY, endY)

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
    if (checkAngle(0)) maxX = Math.max(maxX, 512 + r)
    // 90° (下)
    if (checkAngle(Math.PI / 2)) maxY = Math.max(maxY, 512 + r)
    // 180° (左)
    if (checkAngle(Math.PI)) minX = Math.min(minX, 512 - r)
    // 270° (上)
    if (checkAngle((3 * Math.PI) / 2)) minY = Math.min(minY, 512 - r)

    // 计算边界框中心作为offset
    const centerX = (minX + maxX) / 2
    const centerY = (minY + maxY) / 2

    return { offsetX: centerX, offsetY: centerY }
  }, [arcAngle])

  const x = data.x * 2
  const y = data.y * 2

  return (
    <Group
      x={x}
      y={y}
      rotationDeg={data.angle ?? 0}
      scale={{
        x: scale * (data.horizontalFlip ? -1 : 1),
        y: scale * (data.verticalFlip ? -1 : 1),
      }}
      opacity={opacity}
      clipFunc={clipFunc}
      offsetX={offsetX}
      offsetY={offsetY}
    >
      <Image image={img} width={1024} height={1024} />
    </Group>
  )
}

export default CircleAoe
