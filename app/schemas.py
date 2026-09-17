from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

FirmRole = Literal["FIRM_ADMIN", "ACCOUNTANT"]
ClientRole = Literal["CLIENT_ADMIN", "CLIENT_SUBMITTER"]
UserStatus = Literal["ACTIVE", "DISABLED"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class FirmOut(BaseModel):
    id: UUID
    name: str
    timezone: str


class ClientMembershipOut(BaseModel):
    client_id: UUID
    client_name: str
    role: ClientRole


class PrincipalOut(BaseModel):
    id: UUID
    email: EmailStr
    name: str
    firm: FirmOut
    firm_role: FirmRole | None
    client_memberships: list[ClientMembershipOut]


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: PrincipalOut


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512)
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    password: str = Field(min_length=12, max_length=128)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetRequested(BaseModel):
    message: str
    development_token: str | None = None


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512)
    new_password: str = Field(min_length=12, max_length=128)


class MessageResponse(BaseModel):
    message: str


class StaffInvitationRequest(BaseModel):
    email: EmailStr
    role: FirmRole


class ClientInvitationRequest(BaseModel):
    email: EmailStr
    role: ClientRole


class InvitationOut(BaseModel):
    id: UUID
    email: EmailStr
    role: str
    expires_at: datetime
    token: str


class InvitationListItem(BaseModel):
    id: UUID
    email: EmailStr
    role: str
    expires_at: datetime
    created_at: datetime
    status: Literal["PENDING", "ACCEPTED", "EXPIRED", "REVOKED"]


class InvitationListOut(BaseModel):
    items: list[InvitationListItem]
    total: int
    page: int
    page_size: int


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    status: UserStatus | None = None
    role: FirmRole | None = None


class UserOut(BaseModel):
    id: UUID
    email: EmailStr
    name: str
    status: UserStatus
    firm_role: FirmRole | None
    client_roles: list[ClientMembershipOut]
    last_login_at: datetime | None


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class ClientFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uses_payment_platform: bool = False
    has_employee_reimbursement: bool = False
    has_loan: bool = False
    multi_currency: bool = False
    project_based: bool = False
    has_retention: bool = False


class ClientCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    legal_name: str = Field(min_length=1, max_length=200)
    base_currency: str = Field(default="SGD", pattern=r"^[A-Z]{3}$")
    features: ClientFeatures = Field(default_factory=ClientFeatures)


class ClientUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    features: ClientFeatures | None = None
    status: Literal["ACTIVE", "DISABLED"] | None = None

    @model_validator(mode="after")
    def reject_null_updates(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Updated fields cannot be null")
        return self


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    legal_name: str
    base_currency: str
    features: ClientFeatures
    status: Literal["ACTIVE", "DISABLED"]


class ClientListOut(BaseModel):
    items: list[ClientOut]
    total: int
    page: int
    page_size: int


class ClientMemberOut(BaseModel):
    user_id: UUID
    email: EmailStr
    name: str
    status: UserStatus
    role: ClientRole


class ClientMemberUpdate(BaseModel):
    role: ClientRole
    active: bool = True


class AssignmentUpdate(BaseModel):
    user_ids: list[UUID]


class AssignmentOut(BaseModel):
    user_ids: list[UUID]


class BankAccountCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    bank: str = Field(min_length=1, max_length=100)
    account_last4: str = Field(pattern=r"^[0-9]{4}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: UserStatus = "ACTIVE"


class BankAccountUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    bank: str | None = Field(default=None, min_length=1, max_length=100)
    account_last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    status: UserStatus | None = None

    @model_validator(mode="after")
    def reject_null_updates(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Updated fields cannot be null")
        return self


class BankAccountOut(BankAccountCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
