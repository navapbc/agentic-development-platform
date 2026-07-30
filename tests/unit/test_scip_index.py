from services.repository_intelligence.indexing.scip_index import parse_symbol

_PY = "scip-python python agentic-demo-service 0.1 "
_TS = "scip-typescript npm agentic-demo-ts 0.1 "


def _py(descriptor: str):
    return parse_symbol(_PY + descriptor, "agentic-demo-service")


def _ts(descriptor: str):
    return parse_symbol(_TS + descriptor, "agentic-demo-ts")


def test_python_package_descriptor_is_a_module():
    info = _py("app/__init__:")
    assert info.kind == "module"
    assert info.module == "app"
    assert info.qualified == "app"


def test_python_module_descriptor_is_a_module():
    info = _py("`app.main`/__init__:")
    assert info.kind == "module"
    assert info.module == "app.main"
    assert info.qualified == "app.main"
    assert info.name == "main"


def test_python_top_level_function():
    info = _py("`app.main`/create_task().")
    assert info.kind == "function"
    assert info.qualified == "app.main.create_task"
    assert info.name == "create_task"


def test_python_function_parameter():
    info = _py("`app.main`/create_task().(data)")
    assert info.kind == "parameter"


def test_python_class_and_method_and_field():
    klass = _py("`app.models`/Task#")
    assert klass.kind == "class"
    assert klass.qualified == "app.models.Task"

    method = _py("`app.store`/TaskStore#add().")
    assert method.kind == "method"
    assert method.qualified == "app.store.TaskStore.add"
    assert method.name == "add"

    field = _py("`app.models`/Task#id.")
    assert field.kind == "field"
    assert field.qualified == "app.models.Task.id"


def test_python_module_variable():
    info = _py("`app.store`/store.")
    assert info.kind == "variable"
    assert info.qualified == "app.store.store"


def test_typescript_file_namespace_is_a_module():
    info = _ts("src/`models.ts`/")
    assert info.kind == "module"
    assert info.module == "src/models.ts"
    assert info.qualified == "src/models.ts"


def test_typescript_top_level_function():
    info = _ts("src/`service.ts`/createTask().")
    assert info.kind == "function"
    assert info.qualified == "src/service.ts.createTask"
    assert info.name == "createTask"


def test_typescript_class_method_and_constructor():
    method = _ts("src/`store.ts`/TaskStore#add().")
    assert method.kind == "method"
    assert method.qualified == "src/store.ts.TaskStore.add"

    ctor = _ts("src/`service.ts`/TaskNotFoundError#`<constructor>`().")
    assert ctor.kind == "method"
    assert ctor.name == "<constructor>"


def test_typescript_type_enum_member_and_variable():
    enum = _ts("src/`models.ts`/Status#")
    assert enum.kind == "class"
    assert enum.qualified == "src/models.ts.Status"

    member = _ts("src/`models.ts`/Status#Done.")
    assert member.kind == "field"
    assert member.qualified == "src/models.ts.Status.Done"

    var = _ts("src/`store.ts`/store.")
    assert var.kind == "variable"
    assert var.qualified == "src/store.ts.store"


def test_local_and_malformed_symbols():
    assert parse_symbol("local 0", "p").kind == "local"
    assert parse_symbol("scip-python python p", "p").kind == "unknown"
