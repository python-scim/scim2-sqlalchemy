"""Commit the changes of each SCIM request of the intranet."""

from scim2_server.applications.asgi import ASGIApplication
from scim2_server.applications.wsgi import WSGIApplication

from scim2_sqlalchemy import AsyncSqlAlchemyStorage
from scim2_sqlalchemy import SqlAlchemyStorage

from .scim import GROUPS
from .scim import USERS


class IntranetApplication(WSGIApplication):
    """Serve the intranet, and commit the changes of each request."""

    def __init__(self, session, provider):
        storage = SqlAlchemyStorage(
            {"User": USERS, "Group": GROUPS}, session, provider=provider
        )
        super().__init__(storage, provider)
        self.session = session

    def serve(self, request):
        try:
            response = super().serve(request)
            if response.status < 500:
                self.session.commit()
            else:
                self.session.rollback()
            return response
        finally:
            self.session.remove()


class AsyncIntranetApplication(ASGIApplication):
    """Serve the intranet asynchronously, and commit the changes of each request."""

    def __init__(self, session, provider):
        storage = AsyncSqlAlchemyStorage(
            {"User": USERS, "Group": GROUPS}, session, provider=provider
        )
        super().__init__(storage, provider)
        self.session = session

    async def serve(self, request):
        try:
            response = await super().serve(request)
            if response.status < 500:
                await self.session.commit()
            else:
                await self.session.rollback()
            return response
        finally:
            await self.session.remove()
