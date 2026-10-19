// gocallers prints the enclosing function of every call to pkg.Name (or bare
// Name inside package pkg) under a root directory, excluding _test.go files.
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func main() {
	root, pkg, name := os.Args[1], os.Args[2], os.Args[3]
	found := map[string]bool{}
	filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		fset := token.NewFileSet()
		f, err := parser.ParseFile(fset, p, nil, 0)
		if err != nil {
			return nil
		}
		for _, d := range f.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Body == nil {
				continue
			}
			fn := fd.Name.Name
			if fd.Recv != nil && len(fd.Recv.List) > 0 {
				t := fd.Recv.List[0].Type
				if s, ok := t.(*ast.StarExpr); ok {
					t = s.X
				}
				if id, ok := t.(*ast.Ident); ok {
					fn = id.Name + "." + fn
				}
			}
			ast.Inspect(fd.Body, func(n ast.Node) bool {
				c, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				switch x := c.Fun.(type) {
				case *ast.SelectorExpr:
					if id, ok := x.X.(*ast.Ident); ok && id.Name == pkg && x.Sel.Name == name {
						found[f.Name.Name+"."+fn] = true
					}
				case *ast.Ident:
					if f.Name.Name == pkg && x.Name == name {
						found[f.Name.Name+"."+fn] = true
					}
				}
				return true
			})
		}
		return nil
	})
	var out []string
	for k := range found {
		out = append(out, k)
	}
	sort.Strings(out)
	fmt.Println(strings.Join(out, "\n"))
}
