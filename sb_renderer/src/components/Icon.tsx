import type { IconType } from 'xiv-strat-board'
import NormalIcon from './NormalIcon'
import type { IconProps } from '../@types/iconProps'
import TextBlock from './TextBlock'
import LineAoe from './LineAoe'
import CircleAoe from './CircleAoe'
import Donut from './Donut'
import LineBlock from './LineBlock'

const Icon: React.FC<IconProps> = ({ data }) => {
  switch (data.type as IconType) {
    case 'line_aoe': {
      return <LineAoe data={data} />
    }
    case 'donut': {
      return <Donut data={data} />
    }
    case 'text': {
      return <TextBlock data={data} />
    }
    case 'line': {
      return <LineBlock data={data} />
    }

    case 'circle_aoe':
    case 'fan_aoe': {
      return <CircleAoe data={data} />
    }
    default:
      return <NormalIcon data={data} />
  }
}

export default Icon
