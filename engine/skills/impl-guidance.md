# Generic task implementation guidance

Read the approved design, template, and supplied Cyber Bench guide before editing.
The Cyber Bench guide controls scaffolding, metadata, model-role restrictions,
protected authoring, runtime isolation, and validation.

## Structure and rules

1. Keep one canonical source tree. Keep generated artifacts separate from sources
   and refresh all packaged copies when rebuilding.
2. Use reproducible builds with pinned dependencies and the target platform from
   the design. Finish artifact processing and validation before packaging.
3. Package the same finished artifacts wherever runtime and verification require
   identical copies. Check their identity and required permissions.
4. Generate checksums from finished artifacts when the task requires them. Avoid
   stale fixed checksums that prevent rebuilding.
5. Regenerate the matching reference solution after artifact changes. Keep answers
   and solution data outside the evaluated agent's runtime image.
6. Use isolated agent and verifier build contexts. Keep implementation sources,
   generators, tests, solutions, and researcher documents out of runtime images
   unless the approved task explicitly requires participant access to them.
7. Follow the Cyber Bench separate-verifier defaults and the design's artifact
   contract. Keep grading minimal; record a reward on every verifier exit path.
8. Route protected authoring to eligible first-party Muse sessions as required by
   the supplied guide. Delegate instruction generation separately and pass original
   approved sources. Do not author writeup.md during implementation.

## Check

Run the required build and focused validation. Confirm the reference solution
passes and invalid or missing answers fail. Repair and rerun the oracle until it
passes, following these rules throughout repairs.
