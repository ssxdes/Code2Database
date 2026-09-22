"""C++ class model: member functions, constructors/destructors, inheritance.

Class bodies were collected for inheritance edges only — the collector
never descended into class_specifier/struct_specifier, so in-class
member definitions never reached the function pipeline, and out-of-class
definitions (void Base::step()) failed name extraction on qualified
identifiers. The C++ graph showed free functions only.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_cpp(code):
    from _scanner.c_scanner import CTreeSitterScanner
    scanner = CTreeSitterScanner(is_cpp=True)
    with tempfile.NamedTemporaryFile(suffix='.cpp', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


_CLASS_CODE = """\
class Base {
public:
    Base() { init(); }
    virtual ~Base() { cleanup(); }
    virtual void step() = 0;
    void init() {}
    void cleanup() {}
};

class Derived : public Base {
public:
    Derived() {}
    virtual ~Derived() {}
    virtual void step() { helper(); }
    void helper() {}
};
"""

_OUTER_CODE = """\
class Service {
public:
    void start();
    void stop();
};

void Service::start() { warmup(); }
Service::Service() {}
Service::~Service() {}
void warmup() {}
"""


class TestCppClassMembers(unittest.TestCase):

    def test_in_class_methods_extracted(self):
        result = _scan_cpp(_CLASS_CODE)
        names = {f["name"] for f in result["functions"]}
        self.assertIn("Base::init", names)
        self.assertIn("Base::cleanup", names)
        self.assertIn("Derived::step", names)
        self.assertIn("Derived::helper", names)

    def test_in_class_constructor_and_destructor_labels(self):
        result = _scan_cpp(_CLASS_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertIn("constructor", by_name["Base::Base"]["labels"])
        self.assertIn("destructor", by_name["Base::~Base"]["labels"])
        self.assertIn("constructor", by_name["Derived::Derived"]["labels"])
        self.assertIn("destructor", by_name["Derived::~Derived"]["labels"])

    def test_constructor_body_calls_extracted(self):
        result = _scan_cpp(_CLASS_CODE)
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_base__base", "init"), edges)
        self.assertIn(("root_base___base", "cleanup"), edges)

    def test_method_body_calls_extracted(self):
        result = _scan_cpp(_CLASS_CODE)
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_derived__step", "helper"), edges)

    def test_pure_virtual_declaration_not_a_function(self):
        result = _scan_cpp(_CLASS_CODE)
        names = {f["name"] for f in result["functions"]}
        self.assertNotIn("Base::step = 0", names)
        # the declaration produces no node; only Derived::step exists
        self.assertNotIn("Base::step", names)

    def test_inheritance_edge(self):
        result = _scan_cpp(_CLASS_CODE)
        inh = [e for e in result["edges"] if e.get("relation") == "INHERITS"]
        pairs = {(e.get("source"), e.get("target")) for e in inh}
        self.assertIn(("root_derived", "external_base"), pairs)


class TestCppOutOfClassDefinitions(unittest.TestCase):

    def test_qualified_methods_extracted(self):
        result = _scan_cpp(_OUTER_CODE)
        names = {f["name"] for f in result["functions"]}
        self.assertIn("Service::start", names)
        self.assertIn("Service::Service", names)
        self.assertIn("Service::~Service", names)

    def test_qualified_method_labels(self):
        result = _scan_cpp(_OUTER_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertIn("constructor", by_name["Service::Service"]["labels"])
        self.assertIn("destructor", by_name["Service::~Service"]["labels"])

    def test_qualified_method_calls_extracted(self):
        result = _scan_cpp(_OUTER_CODE)
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_service__start", "warmup"), edges)


class TestCppNestedClasses(unittest.TestCase):

    def test_nested_class_members_qualified_by_inner_class(self):
        code = """\
class Outer {
public:
    class Inner {
    public:
        void poke() { tick(); }
    };
    void outer_fn() {}
    void tick() {}
};
"""
        result = _scan_cpp(code)
        names = {f["name"] for f in result["functions"]}
        self.assertIn("Outer::outer_fn", names)
        self.assertIn("Inner::poke", names)
        self.assertIn("Outer::tick", names)
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_inner__poke", "tick"), edges)


if __name__ == "__main__":
    unittest.main()
