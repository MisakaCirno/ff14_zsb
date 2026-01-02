import { decode } from 'xiv-strat-board'
import { Layer, Stage } from 'react-konva'
import Board from './components/Board'
import Icon from './components/Icon'

const code = new URLSearchParams(window.location.search).get('code') || ''
const board = decode(code)

console.log(board)

function App() {
  return (
    <Stage width={1024} height={768}>
      <Layer>
        <Board type={board.boardBackground ?? 'none'} />
        {board.objects.reverse().map((obj, index) => (
          <Icon key={index} data={obj} />
        ))}
      </Layer>
    </Stage>
  )
}

export default App
