"""More > API access: the owner makes, lists and revokes the household's access tokens (D-068).

Signed-in (cookie and CSRF) routes, owner only. The token itself is returned once, by the create call.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from pydantic import Field

from hbudget.db import repo, tokens
from hbudget.db.tokens import Token
from hbudget.web.deps import ApiError, Owner, second_factor
from hbudget.web.routes.api_household import Body

router = APIRouter(prefix='/api/h/{hid}')


def _json(t: Token) -> dict[str, Any]:
    return {'id': t.id, 'name': t.name, 'scope': t.scope, 'hint': t.hint, 'creator': t.creator,
            'created_at': t.created_at, 'expires_at': t.expires_at, 'last_used_at': t.last_used_at,
            'revoked_at': t.revoked_at}


class TokenBody(Body):
    name: Annotated[str, Field(min_length=1, max_length=60)]
    scope: Literal['read', 'write'] = 'read'
    days: Annotated[int, Field(ge=1, le=3650)] | None = None


@router.get('/tokens')
def token_list(m: Owner) -> dict[str, Any]:
    return {'tokens': [_json(t) for t in tokens.list_for(m.conn, m.hid)]}


@router.post('/tokens')
def token_create(body: TokenBody, request: Request, m: Owner) -> dict[str, Any]:
    second_factor(request, m.conn, m.auth.user)
    try:
        tok, secret = tokens.create(m.conn, m.hid, m.user_id, body.name, body.scope, days=body.days)
    except ValueError as e:
        raise ApiError(400, 'invalid', str(e)) from None
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='token.create', row_id=str(tok.id),
               detail={'scope': tok.scope})
    return {'token': _json(tok), 'secret': secret}


@router.delete('/tokens/{token_id}')
def token_revoke(token_id: int, m: Owner) -> dict[str, Any]:
    # no code needed: revoking only takes access away, and a leaked token must be easy to kill from anywhere
    if not tokens.revoke(m.conn, m.hid, token_id):
        raise ApiError(404, 'not_found', 'Not found.')
    repo.audit(m.conn, household_id=m.hid, user_id=m.user_id, action='token.revoke', row_id=str(token_id))
    return {'ok': True}
