import { decode } from 'xiv-strat-board'
import { Layer, Stage } from 'react-konva'
import Board from './components/Board'
import Icon from './components/Icon'
import { useLayoutEffect, useState } from 'react'

const sceneWidth = 1024
const sceneHeight = 768

const debounce = (fn: TimerHandler, initial: number) => {
  let timer: number
  return () => {
    clearTimeout(timer)
    timer = setTimeout(fn, initial)
  }
}

const updateSize = () => {
  // Get container width
  const containerWidth = document.documentElement.clientWidth
  const containerHeight = document.documentElement.clientHeight

  // Calculate scale
  const scaleWidth = containerWidth / sceneWidth
  const scaleHeight = containerHeight / sceneHeight
  const newScale = Math.min(scaleWidth, scaleHeight)

  return newScale
}

function App() {
  const board = getCode()
  const [scale, setScale] = useState(updateSize)

  useLayoutEffect(() => {
    const updateScale = debounce(() => {
      const newScale = updateSize()
      setScale(newScale)
    }, 100)
    updateScale()
    window.addEventListener('resize', updateScale)
  }, [])
  return (
    <Stage
      width={1024}
      height={768}
      style={{
        transform: `scale(${scale})`,
        transformOrigin: 'top left',
      }}
    >
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

function getCode() {
  const defaultCode =
    '[stgy:a2mW7zYpGVGucnON7LpkuDJH66enQBnNYQkCKKUR6lrKMrVuduwvMbQ5lYPO7cdfHNJexQfOqhOOYwu6DnluGxbRieZQbd41xysoX6g-8ue0Z14MAXSqNr+xsHeqFlaZ6P3ng1n6dc1xLH]'
  const code = location.hash.slice(1) || defaultCode

  try {
    const board = decode(code)
    console.log(board)
    return board
  } catch (error) {
    console.error(error)
    console.error('Failed to decode board code, using default.')
    console.error(code)
    return decode(defaultCode)
  }
}
