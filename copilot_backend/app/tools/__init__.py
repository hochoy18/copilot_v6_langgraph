"""Tool-domain services — T12 / #11.

Owns the business rules the admin `/api/v1/admin/tools` router
delegates to. The ToolRepository (T05 / #6) is the thin Mongo wrapper;
this module is where list-filter / search semantics live, so the
repository stays mechanical and the route stays declarative.

Lives under `app.tools` rather than `app.api.admin_tools` to keep the
"service / router / repository" three-layer split consistent with
`app.conversations` (T10 / #40).
"""
