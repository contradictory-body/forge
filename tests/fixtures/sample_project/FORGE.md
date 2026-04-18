# Sample Project

```yaml
architecture:
  layers:
    - types
    - config
    - repository
    - service
    - handler
  cross_cutting:
    - util
    - common

lint:
  command: ""

structural:
  max_file_lines: 500
  max_function_lines: 80
  naming:
    files: snake_case
    classes: PascalCase
  forbidden_patterns:
    - pattern: "print\\("
      exclude: ["cli.py", "terminal_ui.py"]
      message: "Use logging instead of print()"
```
