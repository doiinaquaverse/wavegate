from db import engine, Base
import models

def main():
    print("Creating tables in the database...")
    Base.metadata.create_all(bind=engine)
    print("Done.")

if __name__ == "__main__":
    main()
