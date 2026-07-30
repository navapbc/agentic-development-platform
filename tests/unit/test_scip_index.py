from services.repository_intelligence.indexing.scip_index import (
    ROLE_DEFINITION,
    _occurrence_enclosing,
    _occurrence_range,
    _project_packages,
    _symbol_package,
    parse_symbol,
)

_PY = "scip-python python agentic-demo-service 0.1 "
_TS = "scip-typescript npm agentic-demo-ts 0.1 "
_JAVA = "scip-java maven maven/com.example/task-service 0.1.0 "
_RUBY = "scip-ruby gem agentic-demo-ruby 0.4.7 "


def _py(descriptor: str):
    return parse_symbol(_PY + descriptor, "agentic-demo-service")


def _ts(descriptor: str):
    return parse_symbol(_TS + descriptor, "agentic-demo-ts")


def _ruby(descriptor: str):
    return parse_symbol(_RUBY + descriptor, "agentic-demo-ruby")


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


def test_maven_package_symbol_is_external_without_project_packages():
    info = parse_symbol(_JAVA + "com/example/taskservice/Task#", "agentic-demo-java")
    assert info.is_project is False


def test_maven_package_symbol_is_project_when_package_is_owned():
    packages = {"maven/com.example/task-service"}
    info = parse_symbol(_JAVA + "com/example/taskservice/Task#", "agentic-demo-java", packages)
    assert info.is_project is True


def test_external_maven_dependency_stays_external_with_project_packages():
    packages = {"maven/com.example/task-service"}
    junit = "scip-java maven maven/org.junit.jupiter/junit-jupiter-api 5.10.2 "
    info = parse_symbol(junit + "org/junit/jupiter/api/Assertions#", "agentic-demo-java", packages)
    assert info.is_project is False


def test_symbol_package_extraction():
    task = _JAVA + "com/example/taskservice/Task#"
    assert _symbol_package(task) == "maven/com.example/task-service"
    assert _symbol_package("local 0") is None


def test_ruby_nested_module_and_class_are_types():
    info = _ruby("AgenticDemo#Task#")
    assert info.kind == "class"
    assert info.qualified == "AgenticDemo.Task"
    assert info.name == "Task"


def test_ruby_method_uses_hash_nested_owner():
    info = _ruby("AgenticDemo#Task#with_status().")
    assert info.kind == "method"
    assert info.qualified == "AgenticDemo.Task.with_status"
    assert info.name == "with_status"


def test_ruby_instance_variable_is_a_field():
    info = _ruby("AgenticDemo#Task#`@id`.")
    assert info.kind == "field"
    assert info.name == "@id"


def test_ruby_gem_package_is_project_when_package_matches():
    info = _ruby("AgenticDemo#TaskStore#add().")
    assert info.is_project is True
    assert _symbol_package("scip-java maven") is None


def test_project_packages_collected_from_definition_occurrences():
    task = _JAVA + "com/example/taskservice/Task#"
    junit = "scip-java maven maven/org.junit.jupiter/junit 5.10.2 X#"
    documents = [
        {
            "occurrences": [
                {"symbol": task, "symbol_roles": ROLE_DEFINITION},
                {"symbol": task, "symbol_roles": 0},
                {"symbol": junit, "symbol_roles": 0},
            ]
        }
    ]
    assert _project_packages(documents) == {"maven/com.example/task-service"}


def test_occurrence_range_reads_legacy_flat_shape():
    assert _occurrence_range({"range": [1, 2, 3]}) == [1, 2, 3]


def test_occurrence_range_reads_typed_single_line_shape():
    raw = {"TypedRange": {"SingleLineRange": {"line": 4, "start_character": 2, "end_character": 9}}}
    assert _occurrence_range(raw) == [4, 2, 9]


def test_occurrence_range_reads_typed_multi_line_shape():
    raw = {
        "TypedRange": {
            "MultiLineRange": {
                "start_line": 4,
                "start_character": 2,
                "end_line": 7,
                "end_character": 1,
            }
        }
    }
    assert _occurrence_range(raw) == [4, 2, 7, 1]


def test_occurrence_range_missing_returns_none():
    assert _occurrence_range({}) is None


def test_occurrence_enclosing_reads_legacy_and_typed_shapes():
    assert _occurrence_enclosing({"enclosing_range": [1, 0, 5, 0]}) == [1, 0, 5, 0]
    typed = {
        "TypedEnclosingRange": {
            "SingleLineEnclosingRange": {"line": 3, "start_character": 0, "end_character": 8}
        }
    }
    assert _occurrence_enclosing(typed) == [3, 0, 8]
    assert _occurrence_enclosing({}) is None
