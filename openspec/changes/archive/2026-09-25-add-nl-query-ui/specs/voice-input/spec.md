## Purpose

Lets a user ask a question by speaking. The browser records the audio, a Whisper model running locally on the server transcribes it, and the transcript is then handled exactly like a typed question. No audio leaves the machine.

## ADDED Requirements

### Requirement: Record and transcribe locally
The UI SHALL offer a microphone control that records a spoken question in the browser. When recording stops, the audio SHALL be transcribed on the server by a local Whisper model. The model is set by `WHISPER_MODEL` (default `small`), `WHISPER_DEVICE` (default `cpu`) and `WHISPER_COMPUTE_TYPE` (default `int8`). Transcription MUST NOT send audio to any external service or need an API key. The transcript MUST be shown to the user as the question text and then processed the same way as a typed question.

#### Scenario: Spoken question
- **WHEN** a user records "which modules import requests" and stops recording
- **THEN** the transcript appears as the question and the answer is produced exactly as for the same typed question

#### Scenario: No API key needed
- **WHEN** voice input is used with `LLM_PROVIDER=openrouter` and no `OPENAI_API_KEY`
- **THEN** transcription still works, and no network request carries the audio

### Requirement: Model loading
The Whisper model SHALL be loaded at most once per server process and reused by every session. On first use, while the model loads (downloading it if it isn't cached), the UI MUST show a message saying the transcription model is loading and that this happens on first use only. Downloaded model files MUST be kept in a cache directory so later starts don't download them again.

#### Scenario: First use
- **WHEN** the first voice question is recorded after the server starts and the model is not cached yet
- **THEN** a "Loading transcription model (first run only)" message is shown until the model is ready, and then the transcript appears

#### Scenario: Second use
- **WHEN** a second voice question is recorded in the same server process
- **THEN** no loading message is shown and the model is not loaded again

### Requirement: Availability fallback
If the Whisper model cannot be loaded (for example, no network on the first download, or an invalid `WHISPER_MODEL`), the UI SHALL show an error naming the cause and disable the microphone control for that server process. Text input MUST keep working.

#### Scenario: Model download fails
- **WHEN** the model is not cached and the download fails
- **THEN** the UI shows an error saying voice input is unavailable and why, the microphone is disabled, and typed questions still work

### Requirement: Transcription limits and failures
Recordings longer than 60 seconds or larger than 10 MB SHALL be rejected with a message and not transcribed. An empty or failed transcription MUST show an error and MUST NOT submit a question. Audio MUST NOT be written to disk or kept after it has been transcribed.

#### Scenario: Silent recording
- **WHEN** the transcription of a recording is empty
- **THEN** the UI shows "No speech detected" and no question is submitted

#### Scenario: Transcription error
- **WHEN** transcription raises an error, for example on undecodable audio
- **THEN** the UI shows an error that includes the failure reason and no question is submitted

#### Scenario: Too long
- **WHEN** a recording is 61 seconds long
- **THEN** the UI says recordings are limited to 60 seconds and nothing is transcribed
