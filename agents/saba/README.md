# Saba — autonomous economic research agent

Initial foundation for Saba within Trinity. This folder is source code, not an automatically deployed Trinity agent.

## Modules
- `CLAUDE.md`: agent mission and operating principles.
- `template.yaml`: Trinity agent template metadata.
- `saba/motion.py`: deterministic, smooth cubic Bézier pointer paths.
- `tests/test_motion.py`: standard-library unit tests.

## Run tests
```bash
cd agents/saba
python -m unittest discover -s tests -v
```

## Example
```python
from saba.motion import generate_path
path = generate_path((100, 100), (500, 320), seed=42)
for point in path:
    print(point.t, point.x, point.y)
```

These trajectories are intended for demonstrations, accessibility testing, and authorized UI automation. They do not bypass anti-bot controls. No credentials are stored here. Integration with a browser controller is a future step.
