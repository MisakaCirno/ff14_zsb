import { Image } from 'react-konva'
import useImage from 'use-image'
import type { BackgroundType } from 'xiv-strat-board'
import { getBoardUrl } from '../utils/staticImage'

const boardMap: Record<BackgroundType, string> = {
  none: getBoardUrl('1'),
  checkered: getBoardUrl('2'),
  checkered_circle: getBoardUrl('3'),
  checkered_square: getBoardUrl('4'),
  grey: getBoardUrl('5'),
  grey_circle: getBoardUrl('6'),
  grey_square: getBoardUrl('7'),
}

interface BoardProps {
  type?: BackgroundType
}

const Board: React.FC<BoardProps> = ({ type = 'checkered' }) => {
  const [image] = useImage(boardMap[type])
  return <Image image={image} width={1024} height={768} />
}

export default Board
