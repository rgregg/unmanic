# Trawlarr API

Served under `/trawlarr/api/v2/` — the prefix comes from
`API_URL_PREFIX` in `trawlarr/libs/runtimepaths.py`. The legacy
`/unmanic/api/v2/` prefix is not aliased and returns 404.

## Rules regarding endpoint creation:
1. All data will be returned in JSON format.
1. The success status of the data will be returned using HTTP status codes.
    - 200: for all successfully returned data.
    - 400: for errors caused by the client request. (self.STATUS_ERROR_EXTERNAL)
    - 404: for an incorrectly structured API endpoint. (self.STATUS_ERROR_ENDPOINT_NOT_FOUND)
    - 405: for a request to an API endpoint with a disallowed method. (self.STATUS_ERROR_METHOD_NOT_ALLOWED)
    - 410: for an endpoint this fork has permanently retired. (self.STATUS_ERROR_GONE)
    - 500: status for internal errors and exception handling. (self.STATUS_ERROR_INTERNAL)
1. All unsuccessful return codes listed above should be executed with:
   ```
    self.set_status(self.STATUS_ERROR_INTERNAL, reason="Unable to read privacy policy.")
    self.write_error()
   ```
   This will provide an error message in the format of:
   ```
   {
        "error": "500: Unable to read privacy policy.",
        "error_code": "INTERNAL_ERROR",
        "messages": {}
    }
   ```
1. The returned 'error' message should not be parsed by the client application. This message is subject to change.
   `error_code` is the field to branch on; it is stable.
1. Catch all exceptions with:
    ```
    try:
        ...
    except BaseApiError as bae:
        self.handle_api_error(bae)
        return
    except Exception as e:
        self.handle_unexpected_error(e)
    ```
1. All endpoint functions must be wrapped in a broad exception capture as in the example above.

## The error contract

Every handled failure of this API answers with the same JSON envelope:

| field        | always present | meaning                                                       |
|--------------|----------------|---------------------------------------------------------------|
| `error`      | yes            | `"<status>: <reason>"`. Prose. Do not parse it.               |
| `error_code` | yes            | One of the taxonomy codes below. Stable. Branch on this.      |
| `messages`   | yes            | Per-field request validation errors. `{}` when there are none. |
| `traceback`  | no             | Formatted traceback lines; only when developer mode is on.     |

Individual responses may add fields on top - a retired endpoint (410) also
carries `"retired": true`, which is the contract #21 established - but never
drop the three above.

The taxonomy lives in `API_ERROR_CODES` in `base_api_handler.py`. Each code
maps to exactly one HTTP status:

| `error_code`         | status | raised when                                         |
|----------------------|--------|-----------------------------------------------------|
| `INVALID_JSON`       | 400    | the request body was not decodable JSON             |
| `VALIDATION_FAILED`  | 400    | the body decoded but failed the request schema      |
| `BAD_REQUEST`        | 400    | the endpoint rejected the request on its content    |
| `ENDPOINT_NOT_FOUND` | 404    | no route matched the URI                            |
| `METHOD_NOT_ALLOWED` | 405    | a route matched, but not for this HTTP method       |
| `ENDPOINT_RETIRED`   | 410    | the endpoint is permanently gone (see #21)          |
| `INTERNAL_ERROR`     | 500    | the server failed; the request may have been fine   |

Adding a code is a compatible change. Changing the status a code maps to is not.

Three rules hold this together, and all three are tested:

1. **`BaseApiError` never writes a response.** It carries the status, the code
   and any validation messages; the `except BaseApiError` that catches it calls
   `handle_api_error()`, and that is the only place it becomes HTTP. Raising and
   writing in two places is how a failure path ends up writing twice.
2. **Every response goes through `finish_once()`.** If the request is already
   finished, the second write is dropped and logged rather than raising -
   because a secondary response masks the real failure it followed (#19).
3. **`dispatch_route()` is the backstop.** A handler that forgets its try/except
   still answers with this envelope rather than Tornado's HTML error page.
