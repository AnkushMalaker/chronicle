declare module 'react-gantt-timeline' {
  import { ComponentType } from 'react'

  export interface TimelineTask {
    id: string
    name: string
    start: Date
    end: Date
    color?: string
  }

  export interface TimelineConfig {
    header?: {
      top?: {
        style?: React.CSSProperties
      }
      middle?: {
        style?: React.CSSProperties
      }
      bottom?: {
        style?: React.CSSProperties
      }
    }
    taskList?: {
      title?: string
      label?: {
        width?: string
      }
      columns?: Array<{
        id: number
        title: string
        fieldName: string
        width: number
      }>
    }
  }

  export interface TimelineProps {
    data: TimelineTask[]
    config?: TimelineConfig
  }

  const Timeline: ComponentType<TimelineProps>
  export default Timeline
}
