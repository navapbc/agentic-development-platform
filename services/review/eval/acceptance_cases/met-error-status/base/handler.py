def get_task(store, task_id):
    task = store.find(task_id)
    return response(200, task)
