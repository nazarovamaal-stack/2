class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def payload(self):
        result = {'error_code': self.code, 'message': self.message}
        if self.details is not None:
            result['details'] = self.details
        return result
