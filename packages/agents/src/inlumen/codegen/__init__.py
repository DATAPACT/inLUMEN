"""inLUMEN's code generator: turns a pipeline graph into runnable Task packages.

Other programs call :func:`inlumen.codegen.run.generate`; the codegen service
calls :func:`inlumen.codegen.generator.generate_pipeline_script_bundles`
directly. Both run the same planning, generation, repair and validation.
"""
