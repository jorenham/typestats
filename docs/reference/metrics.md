# Coverage metrics

Python type annotations are optional: unannotated code runs fine.
As projects grow, missing annotations make it harder for type checkers to catch bugs,
for IDEs to give accurate completions, and for contributors to read the code.

**typestats** measures the *type-coverage* of Python packages: the percentage of public
symbols with type annotations.

## Typable

Each annotation slot is counted individually: function parameters, return types,
variable annotations, class attributes, and method signatures each contribute one slot.
Functions and classes are not counted as single items.

```python
x: int = 0
z: Any = 2


def greet(name: str, greeting: Any) -> str: ...


class Config:
    debug: bool
    timeout: Any
    retries = default_retries()

    def __init__(self, name: str) -> None:
        self.name = name

    def validate(self) -> bool: ...
```

<table data-no-sort>
  <thead>
    <tr>
      <th></th>
      <th>Symbol</th>
      <th>Type</th>
      <th style="text-align: right">typed</th>
      <th style="text-align: right">any</th>
      <th style="text-align: right">typable</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code class="sym-attr">attr</code></td>
      <td><code>x</code></td>
      <td><code>int</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td><code class="sym-attr">attr</code></td>
      <td><code>z</code></td>
      <td><code>Any</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td><code class="sym-func">func</code></td>
      <td><code>greet</code></td>
      <td></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-param">param</code></td>
      <td><code>name</code></td>
      <td><code>str</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-param">param</code></td>
      <td><code>greeting</code></td>
      <td><code>Any</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-return">return</code></td>
      <td></td>
      <td><code>str</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td><code class="sym-class">class</code></td>
      <td><code>Config</code></td>
      <td></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-attr">attr</code></td>
      <td><code>debug</code></td>
      <td><code>bool</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-attr">attr</code></td>
      <td><code>timeout</code></td>
      <td><code>Any</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-attr">attr</code></td>
      <td><code>retries</code></td>
      <td></td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-meth">meth</code></td>
      <td><code>__init__</code></td>
      <td></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
    </tr>
    <tr>
      <td>&#x21B3;&#x21B3; <code class="sym-param">param</code></td>
      <td><code>name</code></td>
      <td><code>str</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3;&#x21B3; <code class="sym-return">return</code></td>
      <td></td>
      <td><code>None</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-attr">attr</code></td>
      <td><code>name</code></td>
      <td></td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td>&#x21B3; <code class="sym-meth">meth</code></td>
      <td><code>validate</code></td>
      <td></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
      <td style="text-align: right"></td>
    </tr>
    <tr>
      <td>&#x21B3;&#x21B3; <code class="sym-return">return</code></td>
      <td></td>
      <td><code>bool</code></td>
      <td style="text-align: right">1</td>
      <td style="text-align: right">0</td>
      <td style="text-align: right">1</td>
    </tr>
    <tr>
      <td></td>
      <td><strong>Total</strong></td>
      <td></td>
      <td style="text-align: right"><strong>10</strong></td>
      <td style="text-align: right"><strong>3</strong></td>
      <td style="text-align: right"><strong>12</strong></td>
    </tr>
  </tbody>
</table>

Note that `self` is excluded because type-checkers do not require it to be annotated.

Unlike mypy and pyright, typestats does not treat types from the stdlib or unknown
third-party packages as `Any`. If a parameter is annotated `x: ArrayLike`, it counts
as typed regardless of whether `ArrayLike` can be resolved.

## Coverage

*Whether* a symbol is annotated is only half the story: `Any` satisfies the type
checker syntactically, yet provides zero type safety.
That is why typestats reports two coverage metrics.

- **Coverage**: `typed / typable`
- **Coverage (strict)**: `(typed - any) / typable`

Non-strict coverage counts `Any` as typed: it shows how much of the API is annotated at
all. Strict coverage excludes `Any`, counting only annotations a type checker can use.
Since `Any` provides no type safety, typestats provides this additional strict coverage metric.

From the example above:

- **Coverage**: 10 / 12 = **83%**
- **Coverage (strict)**: (10 - 3) / 12 = **58%**

The 25-point gap (3 / 12) is the `Any` share of the annotations.
