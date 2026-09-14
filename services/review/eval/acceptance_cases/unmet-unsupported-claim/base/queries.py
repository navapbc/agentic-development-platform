def list_tasks(session, *, limit=50, offset=0):
    query = session.query(Task)
    return query.limit(limit).offset(offset).all()
