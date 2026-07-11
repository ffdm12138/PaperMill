# Final test architecture

The layered migration is complete. Root flat tests and tombstones are
forbidden. Active responsibility layers are `contract`, `security`, `unit`,
`integration`, `hygiene`, `e2e`, and `slow`; behavioral `process` and `stress`
markers may cross responsibility layers and are applied centrally by
`tests/conftest.py`.

The default fast gate explicitly selects contract, security, hygiene, focused
unit coverage, and integration smoke tests while excluding process, slow,
stress, and external tests. Full includes process and slow but excludes stress
and external. Process and stress each have independent gates. The authoritative
commands and test-authoring rules live in `docs/TESTING.md`.

Repository hygiene and runtime-zero packaging use the public behavior in
`src/services/repository_hygiene.py`; tests must validate that behavior rather
than private path-list implementation details.
