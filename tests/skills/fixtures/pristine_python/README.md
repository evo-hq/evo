# math_agent

A tiny "math agent" that answers basic arithmetic questions posed in natural language.

Goal: improve accuracy on a variety of math problems. The current implementation is intentionally naive (only handles addition of two positive integers) and fails on anything else.

## Structure

- `agent/solve.py` — the solver function (this is what we want to optimize)
- `data/problems.jsonl` — a handful of math problems we'd like the solver to get right

## How it's used

```python
from agent.solve import solve
solve("what is 3 plus 4?")  # returns "7"
```

Ideally it should handle subtraction, multiplication, division, word problems, and negative numbers too. Right now it doesn't.
