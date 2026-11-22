from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import findings, loans, models, reviews, training, transit, triage
from .middleware import application_errors, request_history
from .schemas import (CrateInspection, FindingIntake, GallerySetup, LoanIntake, Login, ReopenReview,
                      ReviewDecision, Role, SampleObservation, StaffMember, TransitReservation, Witness)
from .security import issue_token, require, signed_in, verify_password
from .storage import audit, connection, queue

app = FastAPI(title='借展包装档案', version='1.0.0')
app.middleware('http')(application_errors)
app.middleware('http')(request_history)
operator = require(Role.OPERATOR)
supervisor = require(Role.SUPERVISOR)


class Session(BaseModel):
    token: str
    staff: StaffMember


@app.get('/health')
def health():
    with connection() as db, queue() as redis:
        db.execute('SELECT 1 FROM exhibit_loan LIMIT 1')
        redis.ping()
    return {'status': 'ok'}


@app.post('/auth/login', response_model=Session)
def login(data: Login):
    with connection() as db:
        account = db.execute('SELECT * FROM staff WHERE username=%s', (data.username,)).fetchone()
        verified = bool(account and verify_password(data.password, account['password_hash']))
        audit(db, None, data.username, 'login_succeeded' if verified else 'login_failed')
    if not verified:
        raise HTTPException(401, '账号或密码错误')
    staff = StaffMember(**account)
    return Session(token=issue_token(staff), staff=staff)


@app.get('/auth/me', response_model=StaffMember)
def me(staff=Depends(signed_in)):
    return staff


@app.get('/loans')
def list_loans(staff=Depends(signed_in)):
    with connection() as db:
        return loans.archive(db)


@app.get('/loans/{loan_id}')
def loan_detail(loan_id: int, staff=Depends(signed_in)):
    with connection() as db:
        return loans.dossier(db, loan_id)


@app.post('/loans', status_code=201)
def register(data: LoanIntake, staff=Depends(operator)):
    with connection() as db:
        return loans.register(db, data, staff.username)


@app.put('/loans/{loan_id}/crate')
def inspect_crate(loan_id: int, data: CrateInspection, staff=Depends(operator)):
    with connection() as db:
        return loans.inspect_crate(db, loan_id, data, staff.username)


@app.get('/schedule')
def schedule(staff=Depends(signed_in)):
    with connection() as db:
        return transit.schedule(db)


@app.post('/schedule/setup', status_code=201)
def block_gallery(data: GallerySetup, staff=Depends(supervisor)):
    with connection() as db:
        return transit.block_gallery(db, data, staff.username)


@app.post('/loans/{loan_id}/transit', status_code=201)
def reserve_window(loan_id: int, data: TransitReservation, staff=Depends(operator)):
    with connection() as db:
        return transit.reserve(db, loan_id, data, staff.username)


@app.post('/loans/{loan_id}/dispatch')
def dispatch(loan_id: int, staff=Depends(operator)):
    with connection() as db:
        record, denial = transit.dispatch(db, loan_id, staff.username)
    if denial:
        return JSONResponse({'detail': denial}, 409)
    return record


@app.post('/loans/{loan_id}/triage', status_code=202)
def request_score(loan_id: int, staff=Depends(operator)):
    with connection() as db:
        record, case_id = triage.enqueue(db, loan_id, staff.username)
    return record


@app.get('/model/versions')
def model_versions(staff=Depends(signed_in)):
    with connection() as db:
        return {'active': models.active_version(db), 'versions': models.list_versions(db)}


@app.get('/training/jobs')
def training_jobs(staff=Depends(signed_in)):
    with connection() as db:
        return models.list_jobs(db)


@app.post('/loans/{loan_id}/findings', status_code=201)
def record_finding(loan_id: int, data: FindingIntake, staff=Depends(operator)):
    with connection() as db:
        return findings.register(db, loan_id, data, staff.username)


@app.post('/training/jobs', status_code=202)
def start_training(staff=Depends(operator)):
    from .models import TRAINING_QUEUE, notify
    with connection() as db:
        job = training.create_job(db, staff.username)
    notify(TRAINING_QUEUE, job['id'])
    return job


@app.post('/training/jobs/{job_id}/resume', status_code=202)
def resume_training(job_id: int, staff=Depends(operator)):
    from .models import TRAINING_QUEUE, notify
    with connection() as db:
        job = training.resume_job(db, job_id, staff.username)
    notify(TRAINING_QUEUE, job['id'])
    return job


@app.post('/model/versions/{version_id}/publish')
def publish_version(version_id: int, staff=Depends(operator)):
    with connection() as db:
        return models.publish(db, version_id, staff.username)


@app.post('/loans/{loan_id}/review/witness')
def record_witness(loan_id: int, data: Witness, staff=Depends(operator)):
    with connection() as db:
        return reviews.witness(db, loan_id, data, staff.username)


@app.post('/loans/{loan_id}/review/decision')
def review_decision(loan_id: int, data: ReviewDecision, staff=Depends(supervisor)):
    with connection() as db:
        return reviews.decide(db, loan_id, data, staff.username)


@app.post('/loans/{loan_id}/review/sample')
def inspect_sample(loan_id: int, data: SampleObservation, staff=Depends(operator)):
    with connection() as db:
        record, denial = reviews.sample(db, loan_id, data, staff.username)
    if denial:
        return JSONResponse({'detail': denial}, 409)
    return record


@app.post('/loans/{loan_id}/review/reopen')
def reopen_review(loan_id: int, data: ReopenReview, staff=Depends(supervisor)):
    with connection() as db:
        return reviews.reopen(db, loan_id, data, staff.username)
