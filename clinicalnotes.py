import os
import openai
import argparse
import json
import operator
import functools
from copy import deepcopy
from time import sleep, perf_counter
from base64 import b64encode
import base64
from pathlib import Path
from datetime import date
from jinja2 import Environment, PackageLoader, select_autoescape

US_CORE_RACE = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-race'

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Tool for transforming clinical notes in a FHIR Bundle with ChatGPT"
    )
    parser.add_argument(
        "-b",
        "--bundle",
        dest="bundle",
        required=True,
        help="The filename of the input FHIR Bundle",
    )
    args = parser.parse_args()
    if not Path(args.bundle).exists():
        parser.error("Unable to find bundle file: " + args.bundle)
    return args

def parse_bundle_json(bundle_filepath):
    with open(bundle_filepath, "r") as json_fp:
        bundle = json.load(json_fp)
    return bundle

def extract_resources_by_type(bundle, resource_type):
    selected_entries = filter(lambda be: be['resource']['resourceType'] == resource_type, bundle['entry'])
    return list(map(lambda se: se['resource'], selected_entries))

def filter_resources_for_encounter(resources, encounter):
    encounter_id = encounter['id']
    encounter_reference = 'urn:uuid:' + encounter_id
    return filter(lambda resource: resource['encounter']['reference'] == encounter_reference, resources)

def extract_patient(bundle):
    patient_entry = next(filter(lambda be: be['resource']['resourceType'] == 'Patient', bundle['entry']))
    return patient_entry['resource']

def extract_document_reference(bundle, reference_id):
    dr_entry = next(filter(lambda be: be['resource']['id'] == reference_id, bundle['entry']))
    return dr_entry['resource']

def find_encounter(bundle, document_reference):
    encounter_id = document_reference['context']['encounter'][0]['reference']
    encounter_entry = next(filter(lambda be: be['fullUrl'] == encounter_id, bundle['entry']))
    return encounter_entry['resource']

def clean_condition_display(cd):
    return cd.removesuffix('(situation)')\
      .removesuffix('(finding)')\
      .removesuffix('(disorder)')\
      .strip()

def clean_encounter_type_display(etd):
    return etd.removesuffix('(procedure)')\
      .removesuffix('(environment)')\
      .lower()\
      .strip()

def procedure_display(procedure_list):
    procedures = []
    for proc in procedure_list:
        raw_name = proc['code']['coding'][0]['display']
        procedures.append(raw_name.removesuffix('(procedure)').lower().strip())
    return procedures

def extract_race(patient):
    race_extension = next(filter(lambda pe: pe['url'] == US_CORE_RACE, patient['extension']))
    return race_extension['extension'][0]['valueCoding']['display'].lower()

def extract_medication_names(meds, bundle):
    med_names = []
    for med in meds:
        med_cc = med.get('medicationCodeableConcept')
        if med_cc is not None:
            med_names.append(med_cc['coding'][0]['display'])
        else:
            referenced_med_id = med['medicationReference']['reference']
            med_entry = next(filter(lambda be: be['fullUrl'] == referenced_med_id, bundle['entry']))
            med_names.append(med_entry['resource']['code']['coding'][0]['display'])
    return med_names

def build_template_context(patient, encounter, bundle):
    context = {}
    given_name = ' '.join(patient['name'][0]['given'])
    family_name = patient['name'][0]['family']
    context['name'] = given_name + ' ' + family_name
    birth_date = date.fromisoformat(patient['birthDate'])
    encounter_date = date.fromisoformat(encounter['period']['start'][0:10])
    age = (encounter_date - birth_date).days // 365
    context['age'] = age
    context['gender'] = patient['gender']
    encounter_type = encounter['type'][0]['coding'][0]['display']
    context['encounter_type'] = clean_encounter_type_display(encounter_type)
    if 'reasonCode' in encounter:
      reason = encounter['reasonCode'][0]['coding'][0]['display']
      context['reason'] = clean_condition_display(reason)
    context['race'] = extract_race(patient)
    medications = extract_resources_by_type(bundle, 'MedicationRequest')
    encounter_medications = filter_resources_for_encounter(medications, encounter)
    context['medications'] = extract_medication_names(encounter_medications, bundle)
    procedures = extract_resources_by_type(bundle, 'Procedure')
    immunizations = extract_resources_by_type(bundle, 'Immunization')
    encounter_procedures = filter_resources_for_encounter(procedures, encounter)
    context['procedures'] = procedure_display(encounter_procedures)
    encounter_immunizations = filter_resources_for_encounter(immunizations, encounter)
    immunizations = list(map(lambda iz: iz['vaccineCode']['coding'][0]['display'], encounter_immunizations))

    return context

def create_template_environment():
    return Environment(loader=PackageLoader("clinicalnotes"),
                       autoescape=select_autoescape(), trim_blocks=True)

def generate_note(prompt, role, perf_log):
    # Try to call ChatGPT 4 times for this note
    start = perf_counter()
    for attempt in range(1, 5):
        try:
            response = openai.ChatCompletion.create(
                model = "gpt-3.5-turbo",
                messages = [
                    {"role": "system", "content": role},
                    {"role": "user", "content": prompt}
                ]
            )
            ai_generated_note = response['choices'][0]['message']['content']
            complete = perf_counter()
            elapsed_time = complete - start
            perf_log.append({"time": elapsed_time, "attempts": attempt, "success": True})
            return ai_generated_note
        except openai.error.RateLimitError:
            # Sleep longer with each unsuccessful attempt to call the API
            sleep(5 * attempt)
        except openai.error.ServiceUnavailableError:
            # Sleep longer with each unsuccessful attempt to call the API
            sleep(5 * attempt)
        except openai.error.APIError:
            # Sleep longer with each unsuccessful attempt to call the API
            sleep(5 * attempt)
    complete = perf_counter()
    elapsed_time = complete - start
    perf_log.append({"time": elapsed_time, "attempts": attempt, "success": False})
    raise RuntimeError('Unable to generate note after 4 tries.')

def write_output(input_file_name, output_bundle):
    if not os.path.isdir('output'):
        os.mkdir('output')
    basename = os.path.basename(input_file_name)
    output_path = Path('output') / basename
    with open(output_path, "w") as outfile:
        json.dump(output_bundle, outfile)

def main():
    args = parse_arguments()
    bundle = parse_bundle_json(args.bundle)
    # Create a copy of the bundle to write to file so we don't have to mutate
    # the original.
    output_bundle = deepcopy(bundle)
    patient = extract_patient(bundle)
    template_env = create_template_environment()
    openai.api_key = os.getenv("OPENAI_API_KEY")
    encounter_for_problem_template = template_env.get_template('encounter_for_problem.txt.jinja')
    er_template = template_env.get_template('emergency_room.txt.jinja')
    death_cert_template = template_env.get_template('death_certification.txt.jinja')
    oa_template = template_env.get_template('oa_encounter.txt.jinja')
    perf_log = []
    for dr in extract_resources_by_type(bundle, 'DocumentReference'):
        encounter = find_encounter(bundle, dr)
        context = build_template_context(patient, encounter, bundle)
        prompt = None
        system_role = "You are a medical scribe."
        if encounter['type'][0]['coding'][0]['code'] == '50849002':
          prompt = er_template.render(context)
        elif encounter['type'][0]['coding'][0]['code'] == '308646001':
          prompt = death_cert_template.render(context)
          system_role = "You are a medical examiner."
        elif 'reasonCode' in encounter:
            if encounter['reasonCode'][0]['coding'][0]['code'] == '239873007':
                prompt = oa_template.render(context)
            else:
                prompt = encounter_for_problem_template.render(context)

        if prompt is not None:
          ai_generated_note = generate_note(prompt, system_role, perf_log)
    
          encoded_note = b64encode(ai_generated_note.encode('utf-8')).decode()
          decoded_note = base64.b64decode(encoded_note).decode("utf-8")
          reference_id = dr['id']
          output_dr = extract_document_reference(output_bundle, reference_id)
          output_dr['content'][0]['attachment']['data'] = encoded_note
    write_output(args.bundle, output_bundle)
    total_note_count = len(perf_log)
    successful = len(list(filter(lambda row: row["success"] == True, perf_log)))
    failed = len(list(filter(lambda row: row["success"] == False, perf_log)))
    total_time = functools.reduce(operator.add, map(lambda row: row["time"], perf_log))
    average_time = total_time / total_note_count
    print(f'Total notes: {total_note_count}')
    print(f'Success: {successful}')
    print(f'Failed: {failed}')
    print(f'Average time: {average_time}')
    print(decoded_note)
if __name__ == "__main__":
    main()
