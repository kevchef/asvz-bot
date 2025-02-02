#!/usr/bin/python3
# coding=UTF-8

import argparse
import getpass
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.download_manager import WDMDownloadManager
from webdriver_manager.core.http import HttpClient
from webdriver_manager.core.logger import log
from webdriver_manager.core.os_manager import ChromeType

TIMEFORMAT = "%H:%M"

LESSON_BASE_URL = "https://schalter.asvz.ch"

SPORTFAHRPLAN_BASE_URL = "https://asvz.ch/426-sportfahrplan"

CREDENTIALS_FILENAME = ".asvz-bot.json"
CREDENTIALS_ORG = "organisation"
CREDENTIALS_UNAME = "username"
CREDENTIALS_PW = "password"

ETH_ORGANISATION_NAME = "ETH Zürich"
UZH_ORGANISATION_NAME = "Universität Zürich"
ZHAW_ORGANISATION_NAME = "ZHAW - Zürcher Hochschule für Angewandte Wissenschaften"
PHZH_ORGANISATION_NAME = "PH Zürich - Pädagogische Hochschule Zürich"
ASVZ_ORGANISATION_NAME = "ASVZ"
SWITCH_EDUID_ORGANISATION_NAME = "SWITCH edu-ID"

# organisation name as displayed by SwitchAAI
ORGANISATIONS = {
    "ETH": ETH_ORGANISATION_NAME,
    "UZH": UZH_ORGANISATION_NAME,
    "ZHAW": ZHAW_ORGANISATION_NAME,
    "PHZH": PHZH_ORGANISATION_NAME,
    "ASVZ": ASVZ_ORGANISATION_NAME,
    "EDUID": SWITCH_EDUID_ORGANISATION_NAME,
}

WEEKDAYS = {
    "Mo": "Monday",
    "Tu": "Tuesday",
    "We": "Wednesday",
    "Th": "Thursday",
    "Fr": "Friday",
    "Sa": "Saturday",
    "Su": "Sunday",
}

LEVELS = {"Alle": 2104, "Mittlere": 880, "Fortgeschrittene": 726}

FACILITIES = {
    "CAB Move": 45614,
    "Online": 294542,
    "PH Zürich": 45583,
    "Rämibühl": 45573,
    "Rämistrasse 80": 45574,
    "Sport Center Fluntern": 45575,
    "Sport Center Hönggerberg": 45598,
    "Sport Center Irchel": 45577,
    "Sport Center Polyterrasse": 45594,
    "Sport Center Winterthur": 45610,
    "Toni-Areal": 45568,
    "Wädenswil Kraft-/Cardio-Center": 45613,
    "Bad City": 45604,
    "Bad Oerlikon": 45590,
    "Bad Bungertwies": 45602,
}


class EnvVariables:
    """
    Reads asvz-bot specific environment variables.
    Env variable names are prefixed with "ASVZ_", in order to prevent accidental collisions.
    """

    # Enrollment type, e.g. training, lesson, event
    enrollment_type: Optional[str] = os.environ.get("ASVZ_ENROLLMENT_TYPE")

    # Credential values
    cred_organization: Optional[str] = os.environ.get("ASVZ_ORGANIZATION")
    cred_username: Optional[str] = os.environ.get("ASVZ_USERNAME")
    cred_password: Optional[str] = os.environ.get("ASVZ_PASSWORD")
    save_credentials: Optional[str] = os.environ.get("ASVZ_SAVE_CREDENTIALS")

    # Lesson values
    lesson_id: Optional[str] = os.environ.get("ASVZ_LESSON_ID")

    # Training values
    week_day: Optional[str] = os.environ.get("ASVZ_WEEKDAY")
    start_time: Optional[str] = os.environ.get("ASVZ_START_TIME")
    facility: Optional[str] = os.environ.get("ASVZ_FACILITY")
    level: Optional[str] = os.environ.get("ASVZ_LEVEL")
    sport_id: Optional[str] = os.environ.get("ASVZ_SPORT_ID")
    trainer: Optional[str] = os.environ.get("ASVZ_TRAINER")


logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

ISSUES_URL = "https://github.com/fbuetler/asvz-bot/issues"
NO_SUCH_ELEMENT_ERR_MSG = f"Element on website not found! This may happen when the website was updated recently. Please report this incident to: {ISSUES_URL}"

LESSON_ENROLLMENT_NUMBER_REGEX = re.compile(r".*Du\shast\sdie\sPlatz\-Nr\.\s(\d+).*")



def slow_typing(element, text, delay=0.1):
    """Send keys to an element with a delay between each keystroke."""
    for char in text:
        element.send_keys(char)
        time.sleep(delay)  # Adjust delay if needed

class AsvzBotException(Exception):
    pass


class CustomHttpClient(HttpClient):
    def __init__(self, proxy) -> None:
        super().__init__()
        self.proxy = proxy

    def get(self, url, params=None, **kwargs) -> Response:
        """
        Add you own logic here like session or proxy etc.
        """
        log("The call will be done with custom HTTP client")

        if self.proxy:
            # If a proxy is provided, use it
            session = requests.Session()
            session.mount("http://", HTTPAdapter(max_retries=3))
            session.mount("https://", HTTPAdapter(max_retries=3))
            session.proxies = {"http": self.proxy, "https": self.proxy}
            response = session.get(
                url, params=params, **kwargs
            )  # Use params as a keyword argument
        else:
            # If no proxy is provided, make a regular request
            response = requests.get(url, params=params, **kwargs)

        return response


class CredentialsManager:
    def __init__(self, org, uname, password, save_credentials):
        self.credentials = self.__load()
        if self.credentials is None:
            if org is None or uname is None:
                raise AsvzBotException("Not all required credentials are supplied")

            logging.info("Loading credentials from arguments")
            if password is None:
                password = getpass.getpass("Organisation password:")

            self.credentials = {
                CREDENTIALS_ORG: ORGANISATIONS[org],
                CREDENTIALS_UNAME: uname,
                CREDENTIALS_PW: password,
            }
        elif org is not None or uname is not None:
            logging.info(
                "Overwriting credentials loaded from local store with arguments"
            )
            if org is not None:
                self.credentials[CREDENTIALS_ORG] = ORGANISATIONS[org]
            if uname is not None:
                self.credentials[CREDENTIALS_UNAME] = uname

            if password is None:
                password = getpass.getpass("Organisation password:")
            if password is not None and len(password) > 0:
                self.credentials[CREDENTIALS_PW] = password
        else:
            logging.info("Loaded credentials from local store")

        if save_credentials:
            logging.info("Storing credentials in local store")
            self.__store()

    def get(self):
        return self.credentials

    def __store(self):
        with open(CREDENTIALS_FILENAME, "w") as f:
            json.dump(
                self.credentials,
                f,
            )

    def __load(self):
        creds = Path(CREDENTIALS_FILENAME)
        if not creds.is_file():
            return None

        with open(CREDENTIALS_FILENAME, "r") as f:
            data = json.load(f)
            if (
                CREDENTIALS_ORG not in data
                or CREDENTIALS_UNAME not in data
                or CREDENTIALS_PW not in data
            ):
                return None
            return data


class AsvzEnroller:
    @classmethod
    def from_lesson_attributes(
        cls,
        chromedriver_path,
        weekday,
        start_time,
        trainer,
        facility,
        level,
        sport_id,
        proxy_url,
        creds,
    ):
        today = datetime.today()
        weekday_int = time.strptime(WEEKDAYS[weekday], "%A").tm_wday
        weekday_date = today + timedelta((weekday_int - today.weekday()) % 7)
        if level is not None:
            str_level = f"f[2]=niveau:{LEVELS[level]}&"
        else:
            str_level = ""
        sport_url = (
            f"{SPORTFAHRPLAN_BASE_URL}?"
            + f"f[0]=sport:{sport_id}&"
            + f"f[1]=facility:{FACILITIES[facility]}&"
            + str_level
            + f"date={weekday_date.year}-{weekday_date.month:02d}-{weekday_date.day:02d}%20{start_time.hour:02d}:{start_time.minute:02d}"
        )
        logging.info("Searching lesson on '{}'".format(sport_url))

        lesson_url = None
        driver = None
        try:
            driver = AsvzEnroller.get_driver(chromedriver_path, proxy_url)
            driver.get(sport_url)
            driver.implicitly_wait(3)

            day_ele = driver.find_element(
                By.XPATH, "//div[@class='teaser-list-calendar__day']"
            )

            if trainer:
                lesson = day_ele.find_element(
                    By.XPATH,
                    ".//li[@class='btn-hover-parent'][contains(., '{}')]".format(
                        trainer,
                    ),
                )
            else:
                lesson = day_ele.find_element(
                    By.XPATH, ".//li[@class='btn-hover-parent']"
                )
            logging.debug("Found lesson")

            lesson_url = lesson.find_element(
                By.XPATH, ".//a[starts-with(@href, '{}')]".format(LESSON_BASE_URL)
            ).get_attribute("href")
            logging.debug(f"Found lesson url: {lesson_url}")

            # When there is no lesson on the requested day, the ASVZ webpage returns the first lesson on the next day with lessons.
            driver.get(lesson_url)
            driver.implicitly_wait(3)
            lesson_start = AsvzEnroller.__get_enrollment_and_start_time(driver)[1]
            expected_lesson_start = datetime(
                weekday_date.year,
                weekday_date.month,
                weekday_date.day,
                start_time.hour,
                start_time.minute,
            )
            if lesson_start != expected_lesson_start:
                logging.error(
                    "No lesson on the specified date and time! Most likely, you are trying to enroll on a holiday."
                )
                exit(2)

        except NoSuchElementException as e:
            logging.error(
                "Lesson not found! Make sure the lesson is visible on the above URL and the name of the trainer matches."
            )
            exit(1)
        finally:
            if driver is not None:
                driver.quit()

        return cls(chromedriver_path, lesson_url, creds)

    @staticmethod
    def get_driver(chromedriver_path, proxy_url=None):
        options = Options()
        options.add_argument("--private")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")   
        options.add_argument("--disable-gpu")
        options.add_argument("--headless=new")
        options.add_argument(
            "--no-sandbox"
        )  # Required for running as root user in Docker container
        options.add_argument(
            "--disable-dev-shm-usage"
        )  # Required for running as root user in Docker container
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36")
        options.add_experimental_option("prefs", {"intl.accept_languages": "de"})
        options.add_experimental_option("prefs", {"translate": {"enabled": False}})  # Disable automatic translation
        if proxy_url is not None:
            options.add_argument(f"--proxy-server={proxy_url}")

        return webdriver.Chrome(
            service=Service(chromedriver_path),
            options=options,
        )

    @staticmethod
    def wait_until(enrollment_start):
        current_time = datetime.today()

        logging.info(
            "\n\tcurrent time: {}\n\tenrollment time: {}".format(
                current_time.strftime("%H:%M:%S"), enrollment_start.strftime("%H:%M:%S")
            )
        )

        login_before_enrollment_seconds = 1 * 59
        if (enrollment_start - current_time).seconds > login_before_enrollment_seconds:
            sleep_time = (
                enrollment_start - current_time
            ).seconds - login_before_enrollment_seconds
            logging.info(
                "Sleep for {} seconds until {}".format(
                    sleep_time,
                    (current_time + timedelta(seconds=sleep_time)).strftime("%H:%M:%S"),
                )
            )
            time.sleep(sleep_time)

    def __init__(self, chromedriver, lesson_url, creds, proxy_url=None):
        self.chromedriver = chromedriver
        self.lesson_url = lesson_url
        self.creds = creds
        self.proxy_url = proxy_url

        logging.info(
            "Summary:\n\tOrganisation: {}\n\tUsername: {}\n\tPassword: {}\n\tLesson: {}".format(
                self.creds[CREDENTIALS_ORG],
                self.creds[CREDENTIALS_UNAME],
                "*" * len(self.creds[CREDENTIALS_PW]),
                self.lesson_url,
            )
        )

    def enroll(self):
        logging.info("Checking login credentials")
        try:
            driver = AsvzEnroller.get_driver(self.chromedriver, self.proxy_url)
            driver.get(self.lesson_url)
            driver.implicitly_wait(3)
            self.__organisation_login(driver)
            (
                self.enrollment_start,
                self.lesson_start,
            ) = AsvzEnroller.__get_enrollment_and_start_time(driver)
        except NoSuchElementException as e:
            logging.error(NO_SUCH_ELEMENT_ERR_MSG)
            raise e
        finally:
            if driver is not None:
                driver.quit()

        if datetime.today() < self.enrollment_start:
            AsvzEnroller.wait_until(self.enrollment_start)

        try:
            driver = AsvzEnroller.get_driver(self.chromedriver, self.proxy_url)
            driver.get(self.lesson_url)
            driver.implicitly_wait(3)

            logging.info("Starting enrollment")

            enrolled = False
            while not enrolled:
                if self.enrollment_start < datetime.today():
                    logging.info(
                        "Enrollment is already open. Checking for available places."
                    )
                    self.__wait_for_free_places(driver)

                logging.info("Lesson has free places")

                self.__organisation_login(driver)

                try:
                    logging.info("Waiting for enrollment")
                    WebDriverWait(driver, 5 * 60).until(
                        EC.element_to_be_clickable(
                            (
                                By.XPATH,
                                "//button[@id='btnRegister' and (@class='btn-primary btn enrollmentPlacePadding' or @class='btn btn-default')]",
                            )
                        )
                    ).click()

                    time.sleep(5)
                except TimeoutException as e:
                    logging.info(
                        "Place was already taken in the meantime. Rechecking for available places."
                    )
                    continue

                logging.info("Submitted enrollment request.")
                enrolled = True

                try:
                    enrollment_el = driver.find_element(
                        By.TAG_NAME, "app-lessons-enrollment-button"
                    )

                    alert_el = enrollment_el.find_element(
                        By.XPATH, "//div[contains(@class, 'alert')]"
                    )
                    alert_text = alert_el.get_attribute("innerHTML")

                    if "Du hast dich erfolgreich eingeschrieben" in alert_text:
                        logging.info("Successfully enrolled. Train hard and have fun!")
                    else:
                        logging.warning(
                            "Enrollment might have not been successful. Please check your E-Mail."
                        )

                    participation_el = enrollment_el.find_element(By.TAG_NAME, "span")
                    participation_text = participation_el.get_attribute("innerHTML")

                    m = LESSON_ENROLLMENT_NUMBER_REGEX.match(participation_text)
                    if m:
                        enrollment_number = m.group(1)
                        logging.info(f"Your enrollment number is {enrollment_number}")
                    else:
                        logging.warning(
                            "Enrollment might have not been successful. Please check your E-Mail."
                        )
                except NoSuchElementException as e:
                    logging.error("Failed to get enrollment result!")
                    raise e

        except NoSuchElementException as e:
            logging.error(NO_SUCH_ELEMENT_ERR_MSG)
            raise e
        finally:
            if driver is not None:
                driver.quit()

    @staticmethod
    def __get_enrollment_and_start_time(driver):
        try:
            try:
                driver.find_element(By.TAG_NAME, "app-page-not-found")
            except NoSuchElementException:
                pass
            else:
                logging.error("Lesson not found! Please check your lesson details")
                raise Exception("Lesson not found")

            enrollment_start = AsvzEnroller.__get_enrollment_time(driver)
            lesson_start = AsvzEnroller.__get_lesson_time(driver)
        except NoSuchElementException as e:
            logging.error(NO_SUCH_ELEMENT_ERR_MSG)
            raise e

        return (enrollment_start, lesson_start)

    @staticmethod
    def __get_enrollment_time(driver):
        try:
            enrollment_interval_raw = driver.find_element(
                By.XPATH, "//dl[contains(., 'Einschreibezeitraum')]/dd"
            )
        except NoSuchElementException as e:
            # If no section called "Einschreibezeitraum" is found, look for "dl" with "Anmeldezeitraum"
            enrollment_interval_raw = driver.find_element(
                By.XPATH, "//dl[contains(., 'Anmeldezeitraum')]/dd"
            )

        # enrollment_interval_raw is like 'Mo, 04.12.2023 10:00 - Di, 26.12.2023 23:59'
        enrollment_start_raw = (
            enrollment_interval_raw.get_attribute("innerHTML")
            .split("-")[0]
            .split(",")[1]
            .strip()
        )

        try:
            enrollment_start = datetime.strptime(enrollment_start_raw, "%d.%m.%Y %H:%M")
        except ValueError as e:
            logging.error(e)
            raise AsvzBotException(
                "Failed to parse enrollment start time: '{}'".format(
                    enrollment_start_raw
                )
            )

        logging.info(
            "Enrollment starts at {}".format(enrollment_start.strftime("%H:%M:%S"))
        )
        return enrollment_start

    @staticmethod
    def __get_lesson_time(driver):
        try:
            lesson_interval_raw = driver.find_element(
                By.XPATH, "//dl[contains(., 'Datum/Zeit')]/dd"
            )
        except NoSuchElementException:
            # If no section called "Datum/Zeit" is found, look for "dt" with "Lektionen"
            lesson_interval_raw = driver.find_element(
                By.XPATH, "//dt[contains(., 'Lektionen')]/following-sibling::dd[0]"
            )

        # lesson_interval_raw is like 'Mo, 10.05.2021 06:55 - 08:05'
        lesson_start_raw = (
            lesson_interval_raw.get_attribute("innerHTML")
            .split("-")[0]
            .split(",")[1]
            .strip()
        )

        try:
            lesson_start = datetime.strptime(lesson_start_raw, "%d.%m.%Y %H:%M")
        except ValueError as e:
            logging.error(e)
            raise AsvzBotException(
                "Failed to parse lesson start time: '{}'".format(lesson_start_raw)
            )

        logging.info("Lesson starts at {}".format(lesson_start.strftime("%H:%M:%S")))
        return lesson_start

    def __organisation_login(self, driver):
        logging.debug("Start login process")
        WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[@class='btn btn-default' and @title='Login'] | //a[@class='btn btn-default' and @title='Login & Anmelden']",
                )
            )
        ).click()

        logging.info("Login to '{}'".format(self.creds[CREDENTIALS_ORG]))
        if self.creds[CREDENTIALS_ORG] == ASVZ_ORGANISATION_NAME:
            self.__organisation_login_asvz(driver)
        else:
            WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[@class='btn btn-warning btn-block' and @title='SwitchAai Account Login']",
                    )
                )
            ).click()

            logging.info("NOW CHOOSING ORGANISATION")

            submit_button = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='submit' and @value='Select']"))
            )

            organization = driver.find_element(
                By.XPATH, "//input[@id='userIdPSelection_iddtext']"
            )
            organization.send_keys("{}a".format(Keys.CONTROL))
            slow_typing(organization, self.creds[CREDENTIALS_ORG])
            # organization.send_keys(self.creds[CREDENTIALS_ORG])
            organization.send_keys(Keys.ENTER)

            # TODO SCK: Should not be needed as the Enter already triggers this
            # submit_button = WebDriverWait(driver, 10).until(
            #     EC.element_to_be_clickable((By.XPATH, "//input[@type='submit' and @value='Select']"))
            # ).click()

            # UZH switched to Switch edu-ID login @see https://github.com/fbuetler/asvz-bot/issues/31
            if (
                self.creds[CREDENTIALS_ORG] == SWITCH_EDUID_ORGANISATION_NAME
                or self.creds[CREDENTIALS_ORG] == UZH_ORGANISATION_NAME
            ):
                self.__organisation_login_switch_eduid(driver)
            else:
                logging.info("Login at current url'{}'".format(driver.current_url))

                self.__organisation_login_default(driver)

        logging.info("Submitted login credentials")
        time.sleep(3)  # wait until redirect is completed

        if not driver.current_url.startswith(LESSON_BASE_URL):
            logging.warning(
                "Authentication might have failed. Current URL is '{}'".format(
                    driver.current_url
                )
            )
        else:
            logging.info("Valid login credentials")

    def __organisation_login_asvz(self, driver):
        driver.find_element(By.XPATH, "//input[@id='AsvzId']").send_keys(
            self.creds[CREDENTIALS_UNAME]
        )
        driver.find_element(By.XPATH, "//input[@id='Password']").send_keys(
            self.creds[CREDENTIALS_PW]
        )

        WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[@type='submit' and text()='Login']",
                )
            )
        ).click()

    def __organisation_login_switch_eduid(self, driver):
        driver.find_element(By.XPATH, "//input[@id='username']").send_keys(
            self.creds[CREDENTIALS_UNAME]
        )

        WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[@type='submit' and @id='login-button']",
                )
            )
        ).click()

        try:
            driver.find_element(By.XPATH, "//input[@id='password']").send_keys(
                self.creds[CREDENTIALS_PW]
            )
        except NoSuchElementException:
            logging.error(
                "Failed to insert password. Please ensure that your username is an email address."
            )
            exit(1)

        WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[@type='submit' and @id='login-button']",
                )
            )
        ).click()

    def __organisation_login_default(self, driver):
        submit_button = WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.XPATH, "//button[@type='submit']"))
        )

        # driver.find_element(By.XPATH, "//input[@id='username']").send_keys(
        #     self.creds[CREDENTIALS_UNAME]
        # )
        # driver.find_element(By.XPATH, "//input[@id='password']").send_keys(
        #     self.creds[CREDENTIALS_PW]
        # )

        username_field = driver.find_element(By.XPATH, "//input[@id='username']")
        password_field = driver.find_element(By.XPATH, "//input[@id='password']")

        slow_typing(username_field, self.creds[CREDENTIALS_UNAME])
        slow_typing(password_field, self.creds[CREDENTIALS_PW])

        driver.find_element(By.XPATH, "//button[@type='submit']").click()

    def __wait_for_free_places(self, driver):
        while True:
            num_free_spots_raw = driver.find_element(
                By.XPATH, "//dl[contains(., 'Freie Plätze')]/dd/span"
            )
            num_free_spots = int(num_free_spots_raw.get_attribute("innerHTML"))

            if num_free_spots > 0:
                # has free places
                return

            if datetime.today() > self.lesson_start:
                raise AsvzBotException(
                    "Stopping enrollment because lesson has started."
                )

            retry_interval_sec = 1 * 30
            logging.info(
                "Lesson is booked out. Rechecking in {} secs..".format(
                    retry_interval_sec
                )
            )
            time.sleep(retry_interval_sec)
            driver.refresh()


def parse_and_validate_start_time(start_time) -> datetime:
    try:
        return datetime.strptime(start_time, TIMEFORMAT)
    except ValueError:
        msg = "Invalid start time specified. Supported format is {}".format(TIMEFORMAT)
        raise argparse.ArgumentTypeError(msg)


def get_chromedriver_path(proxy_url=None):
    if proxy_url is not None:
        logging.info(f"Using proxy: {proxy_url}")
        http_client = CustomHttpClient(proxy=proxy_url)
        download_manager = WDMDownloadManager(http_client)

    webdriver_manager = None
    try:
        if proxy_url is not None:
            webdriver_manager = ChromeDriverManager(
                chrome_type=ChromeType.CHROMIUM, download_manager=download_manager
            )
        else:
            webdriver_manager = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM)
    except:
        webdriver_manager = None

    if webdriver_manager is None:
        try:
            if proxy_url is not None:
                webdriver_manager = ChromeDriverManager(
                    chrome_type=ChromeType.GOOGLE, download_manager=download_manager
                )
            else:
                webdriver_manager = ChromeDriverManager(chrome_type=ChromeType.GOOGLE)
        except:
            webdriver_manager = None

    if webdriver_manager is None:
        logging.error("Failed to find chrome/chromium")
        exit(1)

    return webdriver_manager.install()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-org",
        "--organisation",
        choices=list(ORGANISATIONS.keys()),
        help="Name of your organisation.",
    )
    parser.add_argument(
        "-u",
        "--username",
        type=str,
        help="Organisation username",
    )
    parser.add_argument(
        "-p",
        "--password",
        type=str,
        help="Organisation password",
    )
    parser.add_argument(
        "-x", "--proxy", type=str, help="Proxy URL", required=False, default=None
    )
    parser.add_argument(
        "--save-credentials",
        default=False,
        action="store_true",
        help="Store your login credentials locally and reused them on the next run",
    )

    subparsers = parser.add_subparsers(
        dest="type", title="Enrollment type", help="Select the enrollment type"
    )

    parser_lesson = subparsers.add_parser("lesson", help="For lessons visited once")
    parser_lesson.add_argument(
        "lesson_id",
        type=int,
        help="ID of a particular lesson e.g. 200949 in https://schalter.asvz.ch/tn/lessons/200949",
    )

    parser_event = subparsers.add_parser("event", help="For one-time events")
    parser_event.add_argument(
        "event_id",
        type=int,
        help="ID of a particular event e.g. 536447 in https://schalter.asvz.ch/tn/events/536447",
    )

    parser_training = subparsers.add_parser(
        "training",
        help="For lessons visited periodically",
    )

    parser_training.add_argument(
        "-w",
        "--weekday",
        required=True,
        choices=list(WEEKDAYS.keys()),
        help="Day of the week of the lesson",
    )

    parser_training.add_argument(
        "-s",
        "--start-time",
        required=True,
        type=parse_and_validate_start_time,
        help="Time when the lesson starts e.g. '19:15'",
    )
    parser_training.add_argument(
        "-t",
        "--trainer",
        required=False,
        type=str,
        help="Trainer giving this lesson",
    )
    parser_training.add_argument(
        "-f",
        "--facility",
        required=True,
        choices=list(FACILITIES.keys()),
        help="Facility where the lesson takes place e.g. 'Sport Center Polyterrasse'",
    )
    parser_training.add_argument(
        "-l",
        "--level",
        required=False,
        choices=list(LEVELS.keys()),
        help="Level of the lesson e.g. 'Alle'",
    )
    parser_training.add_argument(
        "sport_id",
        type=int,
        help="Number at the end of link to a particular sport on ASVZ Sportfahrplan, e.g. 45743 in https://asvz.ch/426-sportfahrplan?f[0]=sport:45743 for volleyball",
    )

    parser.set_defaults(
        organisation=EnvVariables.cred_organization
        if EnvVariables.cred_organization != ""
        else None,
        username=EnvVariables.cred_username
        if EnvVariables.cred_username != ""
        else None,
        password=EnvVariables.cred_password
        if EnvVariables.cred_password != ""
        else None,
        save_credentials=EnvVariables.save_credentials
        if EnvVariables.save_credentials is not None
        else True,
        type=EnvVariables.enrollment_type
        if EnvVariables.enrollment_type != ""
        else None,
        lesson_id=EnvVariables.lesson_id if EnvVariables.lesson_id != "" else None,
        weekday=EnvVariables.week_day if EnvVariables.week_day != "" else None,
        start_time=parse_and_validate_start_time(EnvVariables.start_time)
        if EnvVariables.start_time is not None
        else None,
        trainer=EnvVariables.trainer if EnvVariables.trainer != "" else None,
        facility=EnvVariables.facility if EnvVariables.sport_id != "" else None,
        level=EnvVariables.level if EnvVariables.level != "" else None,
        sport_id=EnvVariables.sport_id if EnvVariables.sport_id != "" else None,
    )

    args = parser.parse_args()
    logging.debug(f"Parsed {args=}")

    creds = None
    try:
        creds = CredentialsManager(
            args.organisation, args.username, args.password, args.save_credentials
        ).get()
    except AsvzBotException as e:
        logging.error(e)
        exit(1)

    # chromedriver_path = get_chromedriver_path(args.proxy)
    chromedriver_path = '/usr/bin/chromedriver'

    enroller = None
    if args.type == "lesson":
        lesson_url = "{}/tn/lessons/{}".format(LESSON_BASE_URL, args.lesson_id)
        enroller = AsvzEnroller(chromedriver_path, lesson_url, creds, args.proxy)
    elif args.type == "event":
        lesson_url = "{}/tn/events/{}".format(LESSON_BASE_URL, args.event_id)
        enroller = AsvzEnroller(chromedriver_path, lesson_url, creds, args.proxy)
    elif args.type == "training":
        enroller = AsvzEnroller.from_lesson_attributes(
            chromedriver_path,
            args.weekday,
            args.start_time,
            args.trainer,
            args.facility,
            args.level,
            args.sport_id,
            args.proxy,
            creds,
        )
    else:
        raise AsvzBotException("Unknown enrollment type: '{}".format(args.type))

    enroller.enroll()


if __name__ == "__main__":
    main()
