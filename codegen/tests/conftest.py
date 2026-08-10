import os

# Individual authentication tests explicitly re-enable the production default.
os.environ["CODEGEN_AUTH_DISABLED"] = "true"
os.environ["CODEGEN_JOB_DB_PATH"] = ":memory:"
os.environ["CODEGEN_ALLOW_DETERMINISTIC_FALLBACK"] = "true"
