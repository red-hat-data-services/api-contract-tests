// Command swagger2openapi converts a Swagger 2.0 document to OpenAPI 3.
//
// The compatibility check compares documents with oasdiff, which reads OpenAPI 3
// only. This command is the conversion step for providers whose contract is a
// Swagger 2.0 document. It uses the same library family as oasdiff
// (github.com/getkin/kin-openapi), at the version pinned in go.mod and go.sum, so
// the conversion is not code this repository maintains.
//
// Usage:
//
//	swagger2openapi <document.json>   convert a file (use - for standard input)
//	swagger2openapi --version         print the tool and library versions
//
// The input must be JSON; the runner converts YAML first. The OpenAPI 3 document
// is written to standard output. Anything that is not a Swagger 2.0 document, and
// any document that cannot be converted (for example one with a dangling
// reference), is an error: the command never writes a partial result.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"runtime/debug"

	"github.com/getkin/kin-openapi/openapi2"
	"github.com/getkin/kin-openapi/openapi2conv"
	"github.com/getkin/kin-openapi/openapi3"
)

const kinModule = "github.com/getkin/kin-openapi"

// version reports the library version this binary was built with, so the runner
// can record it in the report.
func version() string {
	lib := "unknown"
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, dep := range info.Deps {
			if dep.Path == kinModule {
				lib = dep.Version
			}
		}
	}
	return fmt.Sprintf("swagger2openapi kin-openapi %s", lib)
}

// convert turns a Swagger 2.0 JSON document into an OpenAPI 3 JSON document.
func convert(data []byte) ([]byte, error) {
	var probe struct {
		Swagger string `json:"swagger"`
		OpenAPI string `json:"openapi"`
	}
	if err := json.Unmarshal(data, &probe); err != nil {
		return nil, fmt.Errorf("not valid JSON: %w", err)
	}
	if probe.Swagger != "2.0" {
		return nil, fmt.Errorf("not a Swagger 2.0 document (swagger: %q, openapi: %q)", probe.Swagger, probe.OpenAPI)
	}
	var doc openapi2.T
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("cannot read the Swagger 2.0 document: %w", err)
	}
	converted, err := openapi2conv.ToV3(&doc)
	if err != nil {
		return nil, fmt.Errorf("cannot convert to OpenAPI 3: %w", err)
	}
	// A document of definitions only has no paths. OpenAPI 3 requires the object.
	if converted.Paths == nil {
		converted.Paths = openapi3.NewPaths()
	}
	out, err := json.MarshalIndent(converted, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("cannot write the OpenAPI 3 document: %w", err)
	}
	return append(out, '\n'), nil
}

func readInput(name string) ([]byte, error) {
	if name == "-" {
		return io.ReadAll(os.Stdin)
	}
	return os.ReadFile(name)
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 1 && args[0] == "--version" {
		fmt.Fprintln(stdout, version())
		return 0
	}
	if len(args) != 1 {
		fmt.Fprintln(stderr, "usage: swagger2openapi <document.json | ->")
		return 2
	}
	data, err := readInput(args[0])
	if err != nil {
		fmt.Fprintln(stderr, "swagger2openapi:", err)
		return 2
	}
	out, err := convert(data)
	if err != nil {
		fmt.Fprintln(stderr, "swagger2openapi:", err)
		return 1
	}
	_, _ = stdout.Write(out)
	return 0
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
