package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const widgets = `{
  "swagger": "2.0",
  "info": {"title": "Widgets", "version": "1.0"},
  "host": "api.example.com",
  "consumes": ["application/json"],
  "produces": ["application/json"],
  "paths": {
    "/widgets": {
      "get": {
        "parameters": [{"name": "limit", "in": "query", "type": "integer"}],
        "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/Widget"}}}
      },
      "post": {
        "parameters": [{"name": "body", "in": "body", "required": true, "schema": {"$ref": "#/definitions/Widget"}}],
        "responses": {"201": {"description": "created"}}
      }
    }
  },
  "definitions": {"Widget": {"type": "object", "properties": {"name": {"type": "string"}}}}
}`

func decode(t *testing.T, data []byte) map[string]any {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("output is not JSON: %v\n%s", err, data)
	}
	return doc
}

func TestConvertProducesOpenAPI3(t *testing.T) {
	out, err := convert([]byte(widgets))
	if err != nil {
		t.Fatal(err)
	}
	doc := decode(t, out)
	if v, _ := doc["openapi"].(string); !strings.HasPrefix(v, "3.") {
		t.Fatalf("openapi = %v, want 3.x", doc["openapi"])
	}
	if _, ok := doc["swagger"]; ok {
		t.Fatal("swagger key should not remain")
	}
	paths := doc["paths"].(map[string]any)
	post := paths["/widgets"].(map[string]any)["post"].(map[string]any)
	if _, ok := post["requestBody"]; !ok {
		t.Fatal("body parameter was not turned into a requestBody")
	}
	schemas := doc["components"].(map[string]any)["schemas"].(map[string]any)
	if _, ok := schemas["Widget"]; !ok {
		t.Fatal("definitions were not moved to components/schemas")
	}
}

func TestConvertIsDeterministic(t *testing.T) {
	first, err := convert([]byte(widgets))
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		again, err := convert([]byte(widgets))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(first, again) {
			t.Fatal("conversion output differs between runs")
		}
	}
}

func TestDefinitionsOnlyDocumentGetsAnEmptyPathsObject(t *testing.T) {
	out, err := convert([]byte(`{"swagger": "2.0", "info": {"title": "t", "version": "1"},
		"definitions": {"Error": {"type": "object"}}}`))
	if err != nil {
		t.Fatal(err)
	}
	paths, ok := decode(t, out)["paths"].(map[string]any)
	if !ok || len(paths) != 0 {
		t.Fatalf("paths = %v, want an empty object", decode(t, out)["paths"])
	}
}

func TestRejectsDocumentsThatAreNotSwagger2(t *testing.T) {
	for name, input := range map[string]string{
		"openapi3":        `{"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": {}}`,
		"swagger1":        `{"swagger": "1.2"}`,
		"no version":      `{"info": {}}`,
		"not json":        `swagger: "2.0"`,
		"numeric swagger": `{"swagger": 2.0}`,
	} {
		if _, err := convert([]byte(input)); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

func TestDanglingReferenceIsAnErrorNotAPartialResult(t *testing.T) {
	out, err := convert([]byte(`{"swagger": "2.0", "info": {"title": "t", "version": "1"},
		"paths": {"/a": {"get": {"responses": {"200": {"description": "ok",
		"schema": {"$ref": "#/definitions/Missing"}}}}}}}`))
	if err == nil {
		t.Fatalf("expected an error for a reference to an undefined schema, got:\n%s", out)
	}
	if out != nil {
		t.Fatal("a failed conversion must not return a partial document")
	}
	if !strings.Contains(err.Error(), "Missing") {
		t.Fatalf("the error should name the missing definition: %v", err)
	}
}

func TestVersionNamesTheLibrary(t *testing.T) {
	if got := version(); !strings.HasPrefix(got, "swagger2openapi kin-openapi ") {
		t.Fatalf("version() = %q", got)
	}
}

func TestRunConvertsAFileAndReportsErrors(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "good.json")
	if err := os.WriteFile(good, []byte(widgets), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	if code := run([]string{good}, &stdout, &stderr); code != 0 || stdout.Len() == 0 || stderr.Len() != 0 {
		t.Fatalf("code=%d stdout=%d stderr=%q", code, stdout.Len(), stderr.String())
	}
	stdout.Reset()
	stderr.Reset()
	if code := run([]string{filepath.Join(dir, "missing.json")}, &stdout, &stderr); code != 2 || stdout.Len() != 0 {
		t.Fatalf("missing file: code=%d stdout=%d", code, stdout.Len())
	}
	stderr.Reset()
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte(`{"openapi": "3.0.3"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if code := run([]string{bad}, &stdout, &stderr); code != 1 || stdout.Len() != 0 || !strings.Contains(stderr.String(), "not a Swagger 2.0") {
		t.Fatalf("not swagger: code=%d stdout=%d stderr=%q", code, stdout.Len(), stderr.String())
	}
	if code := run(nil, &stdout, &stderr); code != 2 {
		t.Fatalf("no arguments: code=%d", code)
	}
}
