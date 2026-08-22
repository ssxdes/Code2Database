# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability in Code2Database, please report it responsibly:

- **Do not** file a public GitHub issue for security vulnerabilities
- Email the maintainers directly or use GitHub's private vulnerability reporting feature
- Include: description of the vulnerability, steps to reproduce, potential impact

## Security Considerations

Code2Database processes source code using tree-sitter parsers and generates JSON/SQLite output. Key security aspects:

- **No network access required**: The core scanner and builder run entirely locally
- **MCP server mode**: When using `serve`, the stdio transport only accepts local connections
- **Plugin system**: Plugins (`--plugin`) execute arbitrary Python code — only use trusted plugins
- **Git hooks**: The `install-hook` command modifies git configuration — review before using
- **No secrets in output**: Call graph data may contain function names and file paths from your source code, but never extracts string literals or credentials

## Dependency Security

- All dependencies are listed in `scripts/requirements.txt`
- `networkx` and `tree-sitter` are well-maintained, widely-used packages
- Optional `python-igraph` and `leidenalg` are also well-established
- Run `pip audit` periodically to check for known vulnerabilities
