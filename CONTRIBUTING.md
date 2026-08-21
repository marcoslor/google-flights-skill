# Contributing

Issues and PRs are welcome.

- Keep PRs focused; one feature or fix per PR.
- Run the tests before submitting — they are offline and fast:

  ```bash
  python -m unittest discover -s tests
  ```

- The Google Flights RPC notes in the README (`docs/rpc-captures/` holds raw request captures) describe undocumented endpoints; if one changes on Google's side, a capture + failing test is the ideal bug report.
- This fork tracks [AWeirdDev/flights](https://github.com/AWeirdDev/flights) upstream; library-side changes should ideally land upstream first.
