# swagger2openapi

Converts a Swagger 2.0 document to OpenAPI 3, so a Swagger 2.0 contract can be compared by
`oasdiff`, which reads OpenAPI 3 only. The runner calls it for you when a baseline or a
candidate is a Swagger 2.0 document; see
[Swagger 2.0 documents](../../docs/onboarding/openapi-compat.md#swagger-20-documents).

It is a thin wrapper around `openapi2conv` in
[`kin-openapi`](https://github.com/getkin/kin-openapi), the library family `oasdiff` is built
on. The conversion is not code this repository maintains, and the library version is pinned by
`go.mod` and `go.sum`.

```bash
go build -C cmd/swagger2openapi -o "$PWD/bin/swagger2openapi" .   # build
bin/swagger2openapi --version                                   # swagger2openapi kin-openapi v0.153.0
bin/swagger2openapi swagger.json > openapi3.json                # convert (JSON in, JSON out; - reads stdin)
```

The runner converts YAML to JSON first and folds the Swagger `basePath` into the path keys
before calling it, so this program only does the conversion.

## Behavior

- The output is deterministic: the same input always gives the same bytes.
- A document of definitions only (no `paths`) converts with an empty `paths` object, because
  OpenAPI 3 requires one.
- Anything that is not Swagger 2.0 is an error, and so is a document that cannot be converted,
  for example one with a reference to a definition it does not contain. The program never
  writes a partial result: exit status 1 for a failed conversion, 2 for bad usage or an
  unreadable file.

## Development

```bash
go vet -C cmd/swagger2openapi ./...
go test -C cmd/swagger2openapi ./...
```

To change the pinned library, update it with `go get github.com/getkin/kin-openapi@<version>`
inside this directory, run `go mod tidy`, and re-run the Swagger tests under
`modules/openapi-compat/tests/test_swagger2.py`.
