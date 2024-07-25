# Jizzarr <img src=".github/images/jizzarr.png" alt="Jizzarr" width="60"/>

## Introduction
Jizzarr is a web application designed to manage and organize adult content metadata. It has the ability to integrate with both TPDB and Stash.

## Key Features

### 1. Flask-Based Web Application
Jizzarr is built on Flask, a lightweight web framework for Python, providing a robust and scalable backend.

### 2. Database Integration
The application uses SQLAlchemy for ORM (Object-Relational Mapping) and supports SQLite for database management. This ensures efficient data handling and storage.

### 3. API Integration
- **ThePornDB (TPDB) Integration**: Fetches scene details and metadata from TPDB.
- **Stash Integration**: Allows population of sites and scenes from the Stash service.

### 4. Scene Matching
Jizzarr offers a powerful scene matching feature using fuzzy logic and UUID tags. It supports:
- Matching scenes based on title, date, performers, and duration.
- Using custom UUID tags embedded in video files for precise matching.

### 5. Live Updates
The application supports Server-Sent Events (SSE) to provide real-time log updates to the frontend.

### 6. Library Directory Management
Jizzarr allows setting up and managing library directories. It can:
- Scan and match directories with site names.
- Automatically update site directories with new files.

### 7. User Interface
- **Configuration Page**: Set up and manage endpoints, API keys, and library directories.
- **Collection Page**: View and manage the collection of sites and scenes.
- **Statistics Page**: Display comprehensive statistics about the collection.
- **Logs Page**: View and download application logs.

### 8. System Tray Integration
A system tray icon provides easy access to open the application in a browser and quit the application.

### 9. Logging and Error Handling
The application has robust logging and error handling mechanisms, ensuring smooth operation and easy troubleshooting.

## Getting Started

### Prerequisites
- Python 3.8 or later
- pip (Python package installer)

### Installation

1. Clone the repository:
    ```sh
    git clone https://github.com/your-repository/jizzarr.git
    cd jizzarr
    ```

2. Create a virtual environment and activate it:
    ```sh
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3. Install the required packages:
    ```sh
    pip install -r requirements.txt
    ```

### Running the Application

1. Start the application:
    ```sh
    python app.py
    ```

2. Open your browser and navigate to `http://127.0.0.1:6900`.

## Usage

### Configuration
- Navigate to the configuration page to set up Stash and TPDB API keys, endpoints, and download folder.
- Add or remove library directories.

### Collection Management
- View and manage sites and scenes in your collection.
- Add new sites and scenes manually or populate from Stash.

### Scene Matching
- Use the scene matching feature to automatically match video files with scenes in the database.
- View potential matches and manually match scenes if necessary.

### Logs
- Monitor application logs in real-time.
- Download logs for offline review or troubleshooting.

### Statistics
- View detailed statistics about the number of scenes, total and collected duration, average site rating, and more.

## License
This project is licensed under the Unilicense.

## Acknowledgments
- To Gykes and having this crazy inspiration!
- ThePornDB and Stash for their comprehensive adult content databases and media management system.
