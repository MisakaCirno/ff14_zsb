import { Text } from 'react-konva'
import type { IconProps } from '../@types/iconProps'

const TextBlock: React.FC<IconProps> = ({ data }) => {
  const text = data.text ?? ''

  const fontSize = 28
  const textWidth = calcTextWidth(text, fontSize)
  const offsetX = textWidth / 2
  const offsetY = fontSize / 2

  return (
    <Text
      text={data.text}
      fill={data.color}
      x={data.x * 2}
      y={data.y * 2}
      fontSize={fontSize}
      offsetX={offsetX}
      offsetY={offsetY}
      shadowEnabled
      shadowColor="black"
      shadowBlur={4}
      shadowOffset={{
        x: 2,
        y: 2,
      }}
    />
  )
}

export default TextBlock

function calcTextWidth(text: string, fontSize: number) {
  // Approximate width calculation: average character width * number of characters
  const averageCharWidth = fontSize * 0.6 // Rough estimate
  let width = 0
  for (let i = 0; i < text.length; i++) {
    const char = text[i]
    const isAscii = char.charCodeAt(0) < 128
    width += isAscii ? averageCharWidth : averageCharWidth * 2
  }
  return width
}
