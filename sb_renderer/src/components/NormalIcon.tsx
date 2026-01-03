import useImage from 'use-image'
import { getIconConfig } from '../utils/iconMap'
import { getIconUrl } from '../utils/staticImage'
import { Image } from 'react-konva'
import type { IconProps } from '../@types/iconProps'

const NormalIcon: React.FC<IconProps> = ({ data }) => {
  const config = getIconConfig(data)
  if (!config) {
    console.warn(`No icon config found for type: ${data.type}`)
    return null
  }

  const [img] = useImage(getIconUrl(config.src))

  const scale = (data.size ?? 100) / 100
  const opacity = data.hidden ? 0 : (100 - (data.transparency ?? 0)) / 100

  return (
    <Image
      image={img}
      width={config.size * 2}
      height={config.size * 2}
      offsetX={config.size}
      offsetY={config.size}
      x={data.x * 2}
      y={data.y * 2}
      scale={{
        x: scale * (data.horizontalFlip ? -1 : 1),
        y: scale * (data.verticalFlip ? -1 : 1),
      }}
      crop={config.crop}
      rotationDeg={data.angle ?? 0}
      opacity={opacity}
    />
  )
}

export default NormalIcon
