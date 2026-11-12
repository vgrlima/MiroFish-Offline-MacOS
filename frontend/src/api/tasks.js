import service from './index'

export const listTasks = () => service.get('/api/graph/tasks')

export const cancelTask = (taskId) =>
  service.post(`/api/graph/task/${encodeURIComponent(taskId)}/cancel`)

export const resumeTask = (taskId) =>
  service.post(`/api/graph/task/${encodeURIComponent(taskId)}/resume`)

export const clearTaskHistory = () => service.delete('/api/graph/tasks/history')
