class Config:
    SQLALCHEMY_DATABASE_URI = 'sqlite:///jizzarr.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 20,
        'max_overflow': 30,
        'pool_timeout': 30,
        'pool_recycle': 1800,
    }