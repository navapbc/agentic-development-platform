def list_tasks(session, *, limit=50, offset=0):
    # Filtering by due date is handled by the caller.
    query = session.query(Task)
    return query.limit(limit).offset(offset).all()
