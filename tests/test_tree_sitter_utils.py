"""Tests for tree_sitter_utils.py parsing utilities."""

import unittest
from pathlib import Path

from helpers import import_script

tsu = import_script("tree_sitter_utils")

SIMPLE_FUNCTION = b"""\
static PyObject *
my_function(PyObject *self, PyObject *args)
{
    return Py_None;
}
"""

MULTIPLE_FUNCTIONS = b"""\
static int helper(int x) {
    return x + 1;
}

static PyObject *
main_func(PyObject *self, PyObject *args)
{
    int y = helper(42);
    return PyLong_FromLong(y);
}
"""

STATIC_DECLARATIONS = b"""\
static int counter = 0;
static const char *name = "test";
static PyObject *cache = NULL;
static PyTypeObject MyType;
static void my_func(void);
"""

EXTERN_C_BLOCK = b"""\
extern "C" {
static PyObject *
wrapped_func(PyObject *self, PyObject *args)
{
    return Py_None;
}
}
"""

FUNCTION_WITH_CALLS = b"""\
static PyObject *
caller(PyObject *self, PyObject *args)
{
    PyObject *result = PyDict_New();
    PyDict_SetItemString(result, "key", Py_None);
    return result;
}
"""


class TestExtractFunctions(unittest.TestCase):
    """Tests for extract_functions()."""

    def test_simple_function(self):
        """Extracts a single function."""
        tree = tsu.parse_bytes_for_file(SIMPLE_FUNCTION, Path("test.c"))
        funcs = tsu.extract_functions(tree, SIMPLE_FUNCTION)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["name"], "my_function")

    def test_multiple_functions(self):
        """Extracts multiple functions."""
        tree = tsu.parse_bytes_for_file(MULTIPLE_FUNCTIONS, Path("test.c"))
        funcs = tsu.extract_functions(tree, MULTIPLE_FUNCTIONS)
        self.assertEqual(len(funcs), 2)
        names = {f["name"] for f in funcs}
        self.assertEqual(names, {"helper", "main_func"})

    def test_function_has_body(self):
        """Extracted function has body text."""
        tree = tsu.parse_bytes_for_file(SIMPLE_FUNCTION, Path("test.c"))
        funcs = tsu.extract_functions(tree, SIMPLE_FUNCTION)
        self.assertIn("body", funcs[0])
        self.assertIn("return", funcs[0]["body"])

    def test_function_has_start_line(self):
        """Extracted function has start_line."""
        tree = tsu.parse_bytes_for_file(SIMPLE_FUNCTION, Path("test.c"))
        funcs = tsu.extract_functions(tree, SIMPLE_FUNCTION)
        self.assertIn("start_line", funcs[0])
        self.assertGreater(funcs[0]["start_line"], 0)

    def test_no_functions(self):
        """Source with no functions returns empty list."""
        code = b"static int x = 0;\n"
        tree = tsu.parse_bytes_for_file(code, Path("test.c"))
        funcs = tsu.extract_functions(tree, code)
        self.assertEqual(len(funcs), 0)

    def test_function_declaration_not_extracted(self):
        """Forward declarations are not extracted as functions."""
        code = b"static void my_func(void);\n"
        tree = tsu.parse_bytes_for_file(code, Path("test.c"))
        funcs = tsu.extract_functions(tree, code)
        self.assertEqual(len(funcs), 0)


class TestExtractStaticDeclarations(unittest.TestCase):
    """Tests for extract_static_declarations()."""

    def test_static_int(self):
        """Finds static int declaration."""
        tree = tsu.parse_bytes_for_file(STATIC_DECLARATIONS, Path("test.c"))
        decls = tsu.extract_static_declarations(tree, STATIC_DECLARATIONS)
        names = {d["name"] for d in decls}
        self.assertIn("counter", names)

    def test_static_const(self):
        """Finds static const declaration."""
        tree = tsu.parse_bytes_for_file(STATIC_DECLARATIONS, Path("test.c"))
        decls = tsu.extract_static_declarations(tree, STATIC_DECLARATIONS)
        name_decl = [d for d in decls if d["name"] == "name"]
        self.assertTrue(len(name_decl) > 0)
        self.assertTrue(name_decl[0]["is_const"])

    def test_static_pyobject(self):
        """Finds static PyObject* declaration."""
        tree = tsu.parse_bytes_for_file(STATIC_DECLARATIONS, Path("test.c"))
        decls = tsu.extract_static_declarations(tree, STATIC_DECLARATIONS)
        cache_decl = [d for d in decls if d["name"] == "cache"]
        self.assertTrue(len(cache_decl) > 0)
        self.assertTrue(cache_decl[0]["is_pyobject"])

    def test_static_type_object(self):
        """Finds static PyTypeObject declaration."""
        tree = tsu.parse_bytes_for_file(STATIC_DECLARATIONS, Path("test.c"))
        decls = tsu.extract_static_declarations(tree, STATIC_DECLARATIONS)
        type_decl = [d for d in decls if d["name"] == "MyType"]
        self.assertTrue(len(type_decl) > 0)

    def test_function_declaration_skipped(self):
        """Static function declarations are not extracted."""
        tree = tsu.parse_bytes_for_file(STATIC_DECLARATIONS, Path("test.c"))
        decls = tsu.extract_static_declarations(tree, STATIC_DECLARATIONS)
        names = {d["name"] for d in decls}
        self.assertNotIn("my_func", names)

    def test_no_declarations(self):
        """Source without static declarations returns empty."""
        code = b"int main() { return 0; }\n"
        tree = tsu.parse_bytes_for_file(code, Path("test.c"))
        decls = tsu.extract_static_declarations(tree, code)
        self.assertEqual(len(decls), 0)


class TestFindCallsInScope(unittest.TestCase):
    """Tests for find_calls_in_scope()."""

    def test_finds_calls(self):
        """Finds function calls within a scope."""
        tree = tsu.parse_bytes_for_file(FUNCTION_WITH_CALLS, Path("test.c"))
        funcs = tsu.extract_functions(tree, FUNCTION_WITH_CALLS)
        self.assertEqual(len(funcs), 1)
        calls = tsu.find_calls_in_scope(funcs[0]["body_node"], FUNCTION_WITH_CALLS)
        call_names = {c["function_name"] for c in calls}
        self.assertIn("PyDict_New", call_names)
        self.assertIn("PyDict_SetItemString", call_names)

    def test_no_calls(self):
        """Function with no calls returns empty list."""
        code = b"static int noop(void) { return 0; }\n"
        tree = tsu.parse_bytes_for_file(code, Path("test.c"))
        funcs = tsu.extract_functions(tree, code)
        calls = tsu.find_calls_in_scope(funcs[0]["body_node"], code)
        self.assertEqual(len(calls), 0)

    def test_call_has_line_info(self):
        """Each call has start_line and start_byte."""
        tree = tsu.parse_bytes_for_file(FUNCTION_WITH_CALLS, Path("test.c"))
        funcs = tsu.extract_functions(tree, FUNCTION_WITH_CALLS)
        calls = tsu.find_calls_in_scope(funcs[0]["body_node"], FUNCTION_WITH_CALLS)
        for call in calls:
            self.assertIn("start_line", call)
            self.assertIn("start_byte", call)
            self.assertGreater(call["start_line"], 0)


class TestParseBytesFunctions(unittest.TestCase):
    """Tests for parse_bytes_for_file()."""

    def test_c_file(self):
        """Parses a .c file."""
        tree = tsu.parse_bytes_for_file(b"int x = 0;\n", Path("test.c"))
        self.assertIsNotNone(tree)
        self.assertIsNotNone(tree.root_node)

    def test_h_file(self):
        """Parses a .h file."""
        tree = tsu.parse_bytes_for_file(b"int x;\n", Path("test.h"))
        self.assertIsNotNone(tree)

    def test_empty_source(self):
        """Parses empty source without error."""
        tree = tsu.parse_bytes_for_file(b"", Path("test.c"))
        self.assertIsNotNone(tree)


class TestGetNodeText(unittest.TestCase):
    """Tests for get_node_text()."""

    def test_simple_text(self):
        """Extracts text from a node."""
        code = b"int x = 42;\n"
        tree = tsu.parse_bytes_for_file(code, Path("test.c"))
        # Root node text should be the full source
        text = tsu.get_node_text(tree.root_node, code)
        self.assertIn("int", text)


class TestStripComments(unittest.TestCase):
    """Tests for strip_comments()."""

    def test_inline_comment(self):
        """Strips inline comments."""
        result = tsu.strip_comments("int x; // comment\nint y;")
        self.assertNotIn("comment", result)
        self.assertIn("int x;", result)
        self.assertIn("int y;", result)

    def test_block_comment(self):
        """Strips block comments."""
        result = tsu.strip_comments("int x; /* block */ int y;")
        self.assertNotIn("block", result)
        self.assertIn("int x;", result)
        self.assertIn("int y;", result)

    def test_no_comments(self):
        """Source without comments is unchanged."""
        code = "int x = 0;"
        result = tsu.strip_comments(code)
        self.assertEqual(result.strip(), code)


if __name__ == "__main__":
    unittest.main()
