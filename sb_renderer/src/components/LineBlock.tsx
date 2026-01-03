import { Circle, Group, Line } from 'react-konva'
import type { IconProps } from '../@types/iconProps'

const LineBlock: React.FC<IconProps> = ({ data }) => {
  const startX = data.x * 2
  const startY = data.y * 2
  const endX = (data.endX ?? data.x) * 2
  const endY = (data.endY ?? data.y) * 2
  const opacity = data.hidden ? 0 : (100 - (data.transparency ?? 0)) / 100
  return (
    <Group>
      <Line
        points={[startX, startY, endX, endY]}
        height={data.height}
        stroke={data.color ?? '#ff8000'}
        strokeWidth={(data.height ?? 6) * 2}
        opacity={opacity}
      />
      <Circle
        x={startX}
        y={startY}
        radius={8}
        fill="white"
        opacity={opacity}
        stroke="#43A8D8"
        strokeWidth={2}
      />
      <Circle
        x={endX}
        y={endY}
        radius={8}
        fill="white"
        opacity={opacity}
        stroke="#43A8D8"
        strokeWidth={2}
      />
    </Group>
  )
}

export default LineBlock
