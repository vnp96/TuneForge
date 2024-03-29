import logging
import os
import randomname
from datetime import datetime

import requests
from flask import Flask, render_template, redirect, request, send_file, jsonify
from flask_caching import Cache

from db.DBClass import BorgDB
from db.DBQueries import fetch_microservice_url
from storage import azureStorage

from enum import Enum
import glob


class Microservices(Enum):
    NOTES_CONVERTER = 'notes-converter'
    WAVE_EXPORTER = 'wavexporter'
    SOUND_MOOD = 'soundmood'


SESSION_NAME = randomname.get_name()

downloads_folder = 'downloads/'
if not os.path.exists(downloads_folder):
    os.makedirs(downloads_folder)

app = Flask(__name__)
config = {
    'DEBUG': False,
    'CACHE_TYPE': 'SimpleCache',
    'CACHE_DEFAULT_TIMEOUT': 300
}
app.config.from_mapping(config)
cache = Cache(app)
logging.basicConfig(level=logging.INFO)

dbConnection = BorgDB()
BorgDB.test_db_connection()


@app.route('/')
def home_page():
    remove_all_files(downloads_folder)
    app.logger.info("Clearing azure storage blobs: " +
                    str(azureStorage.clear_blobs(SESSION_NAME, hours= 1)))
    return render_template('home_page.html')


@app.route('/clearcache', methods=['POST'])
def clearcache():
    password = request.values.get('password', '')
    if password != os.environ.get('CACHE_RESET_PWD', ''):
        return redirect('/', code=401)

    print('Clearing cache...')
    cache.clear()
    return redirect('/', code=200)


@app.errorhandler(404)
@app.errorhandler(500)
@app.errorhandler(504)
def error_page(e=None):
    # toDo: build error.html
    # return render_template("error.html", error=e)
    return 'error', 500


@app.route('/download', methods=["POST"])
def download_button():
    notes = request.get_json()
    wav_blob = get_wav_blob(notes)
    if 'Error' in wav_blob:
        app.logger.error('Error: Could not make wav file from notes: '
                         + wav_blob)
        return error_page(Exception(wav_blob))

    local_wav = downloads_folder + wav_blob
    try:
        app.logger.info(
            "trying to download to " + local_wav + " from " + wav_blob)
        azureStorage.download(local_wav, wav_blob)
    except Exception as e:
        app.logger.error(e)
        return 'File download error', 500
    return send_file('../' + local_wav, as_attachment=True)


@app.route('/get_mood', methods=['POST'])
def generate_mood():
    notes = add_junk(request.get_json())
    if not notes:
        app.logger.error('Notes could not be obtained from post request')
        return 'Error: make sure the post request is formatted right.', 422

    wav_blob = get_wav_blob(add_junk(notes))
    if 'Error' in wav_blob:
        app.logger.error('Error: Could not make wav file from notes: ' + wav_blob)
        return 'Error: error in generating wav file from notes.', 500

    mood = get_mood_from_wav(wav_blob)
    if 'Error' in mood:
        return jsonify({"mood": "happy"}), 200
    return jsonify({"mood": mood}), 200


@cache.memoize(1500)
def fetch_microservice(microservice):
    return fetch_microservice_url(microservice)


def get_name(extension):
    current_time = datetime.now().strftime("%m-%d-%H-%M-%S")
    return SESSION_NAME + '-' + current_time + extension.lower()


def get_mood_from_wav(wav_blob):
    mood_endpoint = fetch_microservice(
        Microservices.SOUND_MOOD.value)
    mood_upload_endpoint = mood_endpoint + '/api/blob_upload'
    app.logger.info("Sending request to " + mood_upload_endpoint
                    + " with wav blob: " + str(wav_blob))
    upload_response = requests.post(mood_upload_endpoint,
                                    json={'wav': wav_blob})
    if upload_response.status_code != 200:
        app.logger.error('Error: From Sound Mood Generator - '
                         + str(upload_response.text))
        return 'Error: upload failed to sound mood generator.'

    mood_predict_endpoint = mood_endpoint + '/api/predict'
    predict_response = requests.get(mood_predict_endpoint)
    if predict_response.status_code != 200:
        app.logger.error('Error: From Sound Mood Generator - '
                         + 'Unexpected failure in prediction.')
        return 'Error: mood generation failed. Please investigate.'

    return predict_response.json()[0]


def get_wav_blob(notes):
    notes_response = convert_notes_to_midi(notes)
    app.logger.info("Notes converter response:" + str(notes_response.status_code)
                    + " with text: " + notes_response.text)
    if notes_response.status_code != 201:
        app.logger.error(notes_response.text)
        return 'Error: Notes Converter experienced an unexpected error.'

    wav_response = get_wav_from_midi(notes_response.text)
    app.logger.info("WavExporter response:" + str(wav_response.status_code)
                    + " with text: " + wav_response.text)
    if wav_response.status_code != 201:
        app.logger.error(wav_response.text)
        return ('Error: WavExporter microservice '
                + 'experienced an unexpected error.')
    wav_blob = wav_response.text
    return wav_blob


def convert_notes_to_midi(notes):
    notes_converter_url = fetch_microservice(
        Microservices.NOTES_CONVERTER.value)
    notes_converter_endpoint = notes_converter_url + '/'
    app.logger.info("Sending request to " + notes_converter_endpoint
                    + " with notes: " + str(notes))
    notes['midi_filename'] = get_name('.mid')
    return requests.post(notes_converter_endpoint, json=notes)


def get_wav_from_midi(midi_blob):
    wavexporter_url = fetch_microservice_url(Microservices.WAVE_EXPORTER.value)
    wavexporter_endpoint = wavexporter_url + '/'
    midi_dict = {'midi': midi_blob}

    app.logger.info('Sending request to ' + wavexporter_endpoint
                    + ' with midi blob: ' + str(midi_blob))
    return requests.post(wavexporter_endpoint, json=midi_dict)


def add_junk(notes):
    junk = int(20 * notes['speed'])
    notes['notes'].extend([[] for i in range(junk - len(notes))])
    notes['notes'].append([440])
    return notes


def remove_all_files(folder):
    files = glob.glob(os.curdir + '/' + folder + '*')
    for f in files:
        os.remove(f)
