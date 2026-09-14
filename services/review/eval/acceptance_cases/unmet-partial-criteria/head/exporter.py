def export(rows, fmt):
    if fmt == "csv":
        return to_csv(rows)
    if fmt == "json":
        return to_json(rows)
    raise NotImplementedError("xml")
