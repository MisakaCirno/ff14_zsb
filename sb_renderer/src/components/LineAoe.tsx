import { Rect } from 'react-konva'
import type { IconProps } from '../@types/iconProps'

const LineAoe: React.FC<IconProps> = ({ data }) => {
  const width = data.width ?? 128
  const height = data.height ?? 128
  const scale = (data.size ?? 100) / 100
  const opacity = data.hidden ? 0 : (100 - (data.transparency ?? 0)) / 100

  return (
    <Rect
      x={data.x * 2}
      y={data.y * 2}
      offsetX={width}
      offsetY={height}
      width={width * 2}
      height={height * 2}
      fill={data.color ?? '#ff8000'}
      scaleX={scale}
      scaleY={scale}
      rotationDeg={data.angle ?? 0}
      opacity={opacity}
    />
  )
}

export default LineAoe
