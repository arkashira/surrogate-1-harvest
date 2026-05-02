# vanguard / frontend

### 1. **Diagnosis**
* The Vanguard project lacks a comprehensive solution to handle HF API rate limits, which can block dataset training and hinder the project's progress.
* The current implementation does not utilize the HF CDN bypass strategy, which can download public dataset files without being blocked by API rate limits.
* The project's frontend does not have a mechanism to pre-list file paths once and embed them in the training script, which can reduce the number of API calls and avoid rate limits.
* The project's README is missing, which can make it difficult for new contributors to understand the project's goals and implementation details.

### 2. **Proposed change**
The proposed change is to implement the HF CDN bypass strategy in the frontend by pre-listing file paths once and embedding them in the training script. This can be achieved by modifying the `train.py` file to use the HF CDN bypass strategy.

### 3. **Implementation**
To implement the HF CDN bypass strategy, we can follow these steps:
```python
# Import required libraries
import json
import requests

# Define the repository and file path
repo = "huggingface-projects/vanguard"
file_path = "data/train.json"

# Pre-list file paths once and save to a JSON file
def pre_list_file_paths(repo, file_path):
    url = f"https://huggingface.co/{repo}/tree/main"
    response = requests.get(url)
    file_paths = []
    for file in response.json():
        if file["type"] == "file" and file["path"].startswith(file_path):
            file_paths.append(file["path"])
    with open("file_paths.json", "w") as f:
        json.dump(file_paths, f)

# Embed the file paths in the training script
def embed_file_paths_in_train_script(file_paths):
    with open("train.py", "r") as f:
        train_script = f.read()
    train_script = train_script.replace("FILE_PATHS", str(file_paths))
    with open("train.py", "w") as f:
        f.write(train_script)

# Call the functions to pre-list file paths and embed them in the training script
pre_list_file_paths(repo, file_path)
embed_file_paths_in_train_script(json.load(open("file_paths.json", "r")))

# Modify the train.py file to use the HF CDN bypass strategy
# ...
```
We can also add a README file to the project to provide an overview of the project's goals and implementation details.

### 4. **Verification**
To verify that the implementation works, we can check the following:
* The `file_paths.json` file is generated correctly and contains the pre-listed file paths.
* The `train.py` file is modified correctly to use the HF CDN bypass strategy.
* The training script can download the dataset files from the HF CDN without being blocked by API rate limits.
* The project's README file is generated correctly and provides an overview of the project's goals and implementation details.
