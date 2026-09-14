def get_task(store, task_id):
    task = store.find(task_id)
    if task is None:
        return response(404, {"error": "task not found"})
    return response(200, task)
