INLUMEN audio NLP task bundle

Folders map to the four Task nodes:
1. Speech-to-Text Transcription -> transcription.json
2. Data Anonymization -> anonymized.json
3. NER Tagging -> entities.json
4. Sentiment Analysis -> result.json

All tasks:
- read only direct files from PIPELINE_INPUT_DIR
- write exactly one declared output file directly to PIPELINE_OUTPUT_DIR
- support multiple upstream input files
- do not create port-named subdirectories
- use no required custom environment variables

Speech-to-text defaults:
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_BEAM_SIZE=5
WHISPER_LANGUAGE is auto-detected unless supplied.

Note: faster-whisper must have access to the selected Whisper model. On a first run,
the runtime may need network access to download it unless the model is already cached.
The other three tasks are self-contained and require only Python's standard library.
