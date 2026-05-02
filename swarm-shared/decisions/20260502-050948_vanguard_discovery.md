# vanguard / discovery

### Diagnosis
* The Vanguard project lacks a comprehensive README file, making it challenging for new developers to understand the project's purpose, context, and functionality.
* The absence of a README file hinders the onboarding process for new team members and makes it challenging for them to contribute to the project.
* The project's discovery focus is hindered by the lack of documentation, making it difficult to identify areas for improvement and prioritize tasks.
* The project's commit history and swarm-shared decisions suggest a need for better documentation and knowledge management.
* The project's codebase and patterns suggest a complex system with many moving parts, making it essential to have a clear and concise README file.

### Proposed change
Create a comprehensive README file for the Vanguard project, covering its purpose, context, functionality, and contribution guidelines. The README file should be located in the project's root directory (`/opt/axentx/vanguard/README.md`).

### Implementation
1. Create a new file `README.md` in the project's root directory (`/opt/axentx/vanguard/`).
2. Add the following sections to the README file:
	* Introduction: Briefly describe the project's purpose and context.
	* Getting Started: Provide instructions for setting up the project, including dependencies and environment variables.
	* Contribution Guidelines: Outline the process for contributing to the project, including code reviews and testing.
	* Project Structure: Describe the project's directory structure and key components.
	* Patterns and Lessons Learned: Document the project's patterns and lessons learned, including the ones mentioned in the task description.
3. Use Markdown formatting to make the README file easy to read and navigate.
4. Commit the changes to the repository with a meaningful commit message (e.g., "Add comprehensive README file").

Example README file content:
```markdown
# Vanguard Project
## Introduction
The Vanguard project is a complex system with many moving parts. This README file provides an overview of the project's purpose, context, and functionality.

## Getting Started
To get started with the project, follow these steps:

1. Install dependencies: `pip install -r requirements.txt`
2. Set environment variables: `export SHELL=/bin/bash`
3. Run the project: `bash run.sh`

## Contribution Guidelines
To contribute to the project, follow these steps:

1. Fork the repository: `git fork https://github.com/axentx/vanguard.git`
2. Create a new branch: `git branch feature/new-feature`
3. Commit changes: `git commit -m "Add new feature"`
4. Open a pull request: `git push origin feature/new-feature`

## Project Structure
The project's directory structure is as follows:
```plain
vanguard/
|-- README.md
|-- run.sh
|-- requirements.txt
|-- src/
|   |-- __init__.py
|   |-- main.py
|   |-- utils.py
|-- tests/
|   |-- __init__.py
|   |-- test_main.py
|   |-- test_utils.py
```
## Patterns and Lessons Learned
The project has identified several patterns and lessons learned, including:

* Business research with knowledge-rag pipeline
* Top-hub doc insight
* Opus PR reviewer script exec error
* Active-learning wrapper exec error
* Surrogate-1 training pipeline
* HF API rate limit 429
* HF commit cap 128/hr/repo
* Lightning H200 not in default cloud account
* Existing Lightning Studio reuse
* Dataset-mirror writes mixed-schema files to enriched/
* Mac=CLI rule + heavy compute on remote
* HF CDN bypass
* Pre-list file paths once, embed in training script
* Studio reuse instead of recreate
* Lightning idle stop kills training
* Kaggle KGAT token auth
* Kernels/push 400 "Could not convert to integer" on `id` field
* Kernels/push 403 "Phone verification required"
* HF ZeroGPU requires PRO/Team subscription
```
### Verification
To verify that the README file is effective, check the following:

1. New team members can understand the project's purpose and context.
2. The onboarding process is improved, and new team members can contribute to the project more easily.
3. The project's discovery focus is improved, and areas for improvement are more easily identified.
4. The README file is up-to-date and reflects the project's current state.
5. The project's commit history and swarm-shared decisions are more informative and helpful.
