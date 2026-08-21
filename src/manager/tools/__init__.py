"""Runtime-installed manager tools created through create_manager_tool.

Each file here is a complete tool module with a run() entry (or a custom
entry declared at creation time).  Modules are transactional: created,
verified, tested, registered and trial-called in one atomic operation by
src/manager/tool_factory.py, with code and manifest rolled back together
on any failure.
"""
