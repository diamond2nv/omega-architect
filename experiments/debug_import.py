"""Debug import issue"""
import sys
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_PROJECT = _HERE.parent.parent

print(f"__file__: {__file__}")
print(f"_HERE: {_HERE}")
print(f"_PROJECT: {_PROJECT}")
print(f"sys.path before: {sys.path}")

sys.path.insert(0, str(_PROJECT))

print(f"sys.path after: {sys.path}")

# Try importing step by step
try:
    import omega
    print(f"omega OK, __path__: {omega.__path__}")
except Exception as e:
    print(f"omega FAILED: {e}")

try:
    import omega.resource
    print(f"omega.resource OK, budget in dir: {'budget' in dir(omega.resource)}")
except Exception as e:
    print(f"omega.resource FAILED: {e}")

try:
    import omega.resource.budget
    print(f"omega.resource.budget OK")
except Exception as e:
    print(f"omega.resource.budget FAILED: {e}")

try:
    from omega.resource.budget import BudgetTracker
    print(f"BudgetTracker import OK")
except Exception as e:
    print(f"BudgetTracker FAILED: {e}")
