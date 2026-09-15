# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability in Code2Database, please report it responsibly:

- **Do not** file a public GitHub issue for security vulnerabilities
- Email the maintainers directly or use GitHub's private vulnerability reporting feature
- Include: description of the vulnerability, steps to reproduce, potential impact

## Security Considerations

Code2Database processes source code using tree-sitter parsers and generates JSON/SQLite output. Key security aspects:

- **No network access required**: The core scanner and builder run entirely locally
- **Output sensitivity**: graph data contains function names, file paths and — with the clang backend — source string literals (the `string_literals` table, exposed via `get-string-literals`). Treat the graph directory as source-code-sensitive: secrets present in scanned source land in the database
- **MCP server mode**: `serve` defaults to the stdio transport (local only). The HTTP transport (`--transport http`) supports bearer-token auth (`--token` / `C2D_MCP_TOKEN`), TLS (`--tls-cert/--tls-key`), `--read-only` mode, a client cap (`--max-clients`, default 32) and refuses to bind a public interface without a token
- **Plugin system**: Plugins (`--plugin`) execute arbitrary Python code — only use trusted plugins
- **Git hooks**: The `install-hook` command modifies git configuration — review before using

## Dependency Security

- Core dependencies are listed in `scripts/requirements.txt`; optional capabilities ship as wheel extras in `pyproject.toml` (clang / solver / community / daemon / resources / streaming / neural)
- `networkx` and `tree-sitter` are well-maintained, widely-used packages
- Run `pip audit` periodically to check for known vulnerabilities
